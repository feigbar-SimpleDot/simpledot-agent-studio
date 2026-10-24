import React, { useEffect, useRef, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { gw, makeGw, gwBootstrap } from '../gateway.js'
import { api } from '../api.js'
import { i18next } from '../i18n'
import { Modal } from '../components/ui.jsx'
import { FileDrawer } from '../components/filebrowser/FileDrawer.jsx'
import { enhanceFileLinks } from '../components/filebrowser/chatLinks.js'

// ════════════════════════════════════════════════════════════════════
// ChatView — i18n v1.0 conversion
//
// [en] Gateway chat view (main agent + workers). All user-visible copy
//      (input/session/team-work/slash commands/toolgroup cards/multi-agent
//      card/errors) routes through t(). Server-rendered strings (note/
//      message/error from /api/*) stay server-language (D9). The protocol
//      marker '【Team Work 多智能体任务】' inside parseDispatchCard() is
//      a literal the parser uses to detect dispatch cards — it MUST NOT
//      be translated (D10 protocol literal). The prompt text built in
//      submitTeamWork() also keeps its Chinese guidance lines (those are
//      LLM-readable literals, not UI copy).
// [zh-cn] Gateway 对话视图(main + worker)。所有用户可见文案(输入/会话列表/
//        Team Work/命令提示/工具组卡/多 agent 卡/错误)走 t()。服务端字
//        符串(/api/* 推过来的 note/message/error)保持服务端语言(D9)。
//        parseDispatchCard 内的协议 marker '【Team Work 多智能体任务】'
//        是解析器用来识别下发卡的字面量,不能翻译(D10 协议禁区);
//        submitTeamWork 内给 LLM 看的 prompt 字面也是协议级引导,不动。
// ════════════════════════════════════════════════════════════════════

marked.setOptions({ breaks: true, gfm: true })

// issue #75 B3: 每批「对话内容」条数上限 (FR-08「最多10条」, 与后端
// WANT_DEFAULT 对齐; 后端 clamp 到 [1,100])。
const HIST_WANT = 10

function md(text) {
  return { __html: DOMPurify.sanitize(marked.parse(text || '')) }
}

// 把 gateway /messages 的原始记录转成前端消息模型。
// ts: unix 秒; 后端 m.timestamp 是浮点秒, 缺失时兜底为 0(渲染层用 fmtTime 判空)
// 注意: tool_call / thinking 不做历史重建(用户 2026-10-07 要求)——它们只在实时
// SSE 流里临时渲染(见 runStream/send 的 tool.* 事件), 切页/刷新即丢。
// 历史视图只保留 user 气泡 + assistant 正文, 工具调用压成一行提示。
function historyToMessages(raw) {
  // REOPEN P1-1 / DESIGN §2.4: 历史视图用同款 toolgroup 卡 —— 不再压成
  // "调用工具: x, y" 这种干扰性单行。回合级分组: 同一段 assistant 前后
  // 的 tool/role=tool results 都进同一组, 组已完成 closed=true。条目
  // 状态全部 done (历史).
  //
  // 2026-10-11 fix: 不再「每个独立 assistant message = 新 group」 —— 老
  // hermes gateway 会把每个 tool_call turn 各建一条 assistant 消息, 这
  // 导致一连串 1 次工具调用 × N 张卡 (issue #1 复发)。改成: 只有 assistant
  // 带真 content (文本回复) 或 user 中间插入时才关组; 连续的 assistant
  // tool_calls 同组累加 (实时 SSE 流已合并, 历史 view 也应一致)。
  const out = []
  let cur = null
  const tsOf = (m) => (typeof m.timestamp === 'number' ? m.timestamp : 0)
  const isToolCallAssistant = (m) =>
    m.role === 'assistant' && Array.isArray(m.tool_calls) && m.tool_calls.length > 0
  const hasAssistantText = (m) =>
    m.role === 'assistant' && m.content && m.content.trim().length > 0

  const flush = () => {
    if (cur && cur.items.length > 0) {
      cur.closed = true
      out.push(cur)
    }
    cur = null
  }
  for (const m of raw) {
    if (m.role === 'user') {
      if (m.display_kind === 'command') continue
      flush()
      out.push({ role: 'user', text: m.content || '', ts: tsOf(m) })
      continue
    }
    if (isToolCallAssistant(m)) {
      // 同组累加: 仅在当前 cur 已 closed 时才新开 —— 与实时 pushToolCall
      // 语义对齐 (pushToolCall 在 tail closed 时才新建)。
      if (!cur || cur.closed) {
        flush()
        cur = { role: 'toolgroup', ts: tsOf(m), items: [], closed: false }
      }
      for (const c of m.tool_calls) {
        cur.items.push({
          name: c.function?.name || 'tool',
          args: '',
          result: null,
          status: 'done',  // 历史模式全 done, 折叠态绿点
          ts: tsOf(m) + cur.items.length * 0.001,
        })
      }
      continue
    }
    if (hasAssistantText(m)) {
      // 文本回复 = 回合收口, 关组后追加气泡 (与实时 run.completed 语义对齐)
      flush()
      out.push({ role: 'assistant', text: m.content, ts: tsOf(m) })
      continue
    }
    if (m.role === 'tool' && cur) {
      // 历史 tool 结果: 挂到当前组的最后一条 result=null 的 item 上
      // (gateway 返回可能按 assistant 调用顺序对齐, 也可能跨多个 tool_calls
      // 顺序抵达 —— 保守按 arrival 顺序填)
      const lastEmpty = cur.items.findIndex(it => it.result == null)
      if (lastEmpty >= 0) {
        cur.items[lastEmpty] = {
          ...cur.items[lastEmpty],
          result: m.content || '',
          status: m.is_error ? 'error' : 'done',
        }
      }
    }
  }
  flush()
  return out
}

// issue #75 B3: 代理层窗口条目 (ARCH-75 §5.1 R2: user/assistant 文本 +
// tool_group 附属) → 原始记录形态 → 复用 historyToMessages 同一分组语义。
// 无 tool_group 字段的条目 (后端回退=旧全量响应) 原样透传给旧转换器,
// 零语义漂移 (§9 回退: 参数缺失即旧行为, 前端同款向后兼容)。
function windowedToMessages(raw) {
  const recs = []
  for (const e of raw || []) {
    if (!e || typeof e.tool_group === 'undefined') {
      return historyToMessages(raw)  // 旧全量形态 → 旧路径
    }
    for (const it of e.tool_group) {
      if (it.type === 'tool_call') {
        recs.push({ role: 'assistant', content: '', timestamp: e.timestamp,
                    tool_calls: [{ id: it.id,
                                   function: { name: it.name, arguments: it.arguments } }] })
      } else {
        recs.push({ role: 'tool', content: it.content, is_error: it.is_error,
                    timestamp: e.timestamp })
      }
    }
    recs.push({ role: e.role, content: e.content, id: e.id,
                display_kind: e.display_kind, timestamp: e.timestamp })
  }
  return historyToMessages(recs)
}

function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const hm = d.toTimeString().slice(0, 5)
  return sameDay ? hm : `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`
}

// 工具参数美化: tool.started 的 args 可能是 JSON 字符串, 也可能已是对象
// (gateway 直接透传 parsed dict)。统一: 对象 → JSON.stringify pretty;
// 字符串 → 尝试 parse 再 pretty, 失败原样。截断防止超长命令刷屏。
function fmtToolArgs(raw, max = 1200) {
  if (raw == null || raw === '') return ''
  let s
  if (typeof raw === 'string') {
    s = raw
    try { s = JSON.stringify(JSON.parse(s), null, 2) } catch { /* 非 JSON, 原样 */ }
  } else {
    try { s = JSON.stringify(raw, null, 2) } catch { s = String(raw) }
  }
  // E2 fix: truncation notice routed through t() (locale-aware copy).
  // [zh-cn] 截断提示走 t(), 语言切换即时生效。
  return s.length > max ? s.slice(0, max) + '\n'
    + i18next.t('chat.tool_args_truncated', { n: s.length }) : s
}

// toolgroup: 回合级单卡聚合 (REOPEN-DESIGN §2.2/§2.3①, issue #1 B-AC1)。
// 从尾部向前回溯定位第一个「未 closed」的 toolgroup → 追加条目; 找不到才新建。
// 穿插的 assistant/turnthinking 气泡【不再断组】(旧「相邻合并+穿插关组」规则已删),
// closed 仅由回合结束封组 closeTurnToolGroups 置位 —— 一轮对话所有 tool_call
// 只出一张默认折叠卡。仍只在实时 SSE 流里渲染, 切页/刷新即丢, 不进 historyToMessages。
function pushToolCall(setMessages, d) {
  const item = { name: d.tool_name || 'tool', args: fmtToolArgs(d.args),
                 result: null, status: 'running', ts: Date.now() / 1000 }
  if (typeof window !== 'undefined') {
    window.__pushTrace = (window.__pushTrace || [])
    window.__pushTrace.push({ name: d.tool_name, tcId: d.tool_call_id, ts: Date.now() })
    if (window.__pushTrace.length > 30) window.__pushTrace.shift()
  }
  setMessages((m) => {
    if (typeof window !== 'undefined') {
      const last = m[m.length - 1]
      window.__lastTrace = { len: m.length, lastRole: last?.role, lastClosed: last?.closed, lastItems: last?.items?.length }
    }
    for (let i = m.length - 1; i >= 0; i--) {
      if (m[i].role === 'toolgroup' && !m[i].closed) {
        const c = [...m]
        c[i] = { ...m[i], items: [...m[i].items, item] }
        return c
      }
    }
    return [...m, { role: 'toolgroup', items: [item], ts: item.ts }]
  })
}

// 回合结束封组 (DESIGN §2.3②): 把所有未 closed 的 toolgroup 置 closed=true。
// 幂等: 无未关组时返回同一引用(零更新)。封组后迟到的 tool.started 开新组,
// 不再污染已收口的回合。
function closeTurnToolGroups(setMessages) {
  console.log('[CLOSE] entering closeTurnToolGroups')
  setMessages((m) => {
    if (!m.some(x => x.role === 'toolgroup' && !x.closed)) { console.log('[CLOSE] noop, no unclosed group'); return m }
    console.log('[CLOSE] closing', m.filter(x => x.role === 'toolgroup' && !x.closed).length, 'toolgroups')
    return m.map(x => (x.role === 'toolgroup' && !x.closed) ? { ...x, closed: true } : x)
  })
}

// 从 GET messages 原始数组抽取增量 reasoning (DESIGN §2.3③)。
// seen: Map<msgId|'#idx', consumedLen> 消费游标, 本函数就地推进。
// 返回新增片段 [{key, text}]: 同 key 只回 consumed 之后的尾巴, 防重复渲染。
// 字段兼容: m.reasoning ?? m.reasoning_content; 非字符串 String() 化; 空段跳过。
// startIndex (REOPEN-FIX P1-1): 回合下界 —— GET messages 返回的是**全会话历史**,
// 只处理下界之后的消息, 防历史轮次的 reasoning 被当成本轮新片段渲染。
function extractReasoningSegments(rawMessages, seen, startIndex = 0) {
  const segs = []
  if (!Array.isArray(rawMessages)) return segs
  for (let i = Math.max(0, startIndex | 0); i < rawMessages.length; i++) {
    const mm = rawMessages[i]
    if (!mm || mm.role !== 'assistant') continue
    const r = mm.reasoning ?? mm.reasoning_content
    if (r == null || r === '') continue
    const text = typeof r === 'string' ? r : String(r)
    const key = mm.id != null ? String(mm.id) : '#' + i
    const consumed = seen.get(key) || 0
    if (text.length <= consumed) continue
    segs.push({ key, text: text.slice(consumed) })
    seen.set(key, text.length)
  }
  return segs
}

// 权威收口 (DESIGN §2.3④): transcript 里全部 assistant reasoning 按序拼接,
// 回合末整体覆盖 turnthinking 卡文本, 防轮询漏尾/半截。'' 表示无 reasoning。
function authoritativeReasoning(messages) {
  if (!Array.isArray(messages)) return ''
  const parts = []
  for (const mm of messages) {
    if (!mm || mm.role !== 'assistant') continue
    const r = mm.reasoning ?? mm.reasoning_content
    if (r == null || r === '') continue
    parts.push(typeof r === 'string' ? r : String(r))
  }
  return parts.join('')
}

// 向回合思考卡追加 reasoning 片段 (DESIGN §2.4): 已有 streaming 中的思考卡则
// 不可变追加(一张卡持续生长), 没有才新建。无 reasoning 时永不被调用 → 不渲染。
function appendTurnThinking(setMessages, chunk) {
  setMessages((m) => {
    for (let i = m.length - 1; i >= 0; i--) {
      if (m[i].role === 'turnthinking' && m[i].streaming) {
        const c = [...m]
        c[i] = { ...m[i], text: (m[i].text || '') + chunk }
        return c
      }
    }
    return [...m, { role: 'turnthinking', text: chunk, streaming: true, ts: Date.now() / 1000 }]
  })
}

// 回合末思考卡收口 (DESIGN §2.4): auth 非空 → 权威全文覆盖(防轮询漏尾);
// auth 为空且卡已存在 → 保留轮询已得内容; auth 空且无卡 → 不创建(B-AC5 不造假)。
// 无论哪条路径, streaming 一律置 false。
function sealTurnThinking(setMessages, auth) {
  setMessages((m) => {
    const i = m.findIndex(x => x.role === 'turnthinking' && x.streaming)
    if (i >= 0) {
      const c = [...m]
      c[i] = auth ? { ...m[i], text: auth, streaming: false } : { ...m[i], streaming: false }
      return c
    }
    if (auth) return [...m, { role: 'turnthinking', text: auth, streaming: false, ts: Date.now() / 1000 }]
    return m
  })
}

// ── 回合流 handler 公共工厂 (DESIGN D6, issue #1 B-AC4) ──
// runStream 与 send 共用同一份事件处理代码: 回合级单卡聚合、reasoning 1.5s
// 轮询、回合末权威收口全部住在这里, 不再有两套 switch 漏改风险。
// deps: { setMessages, setRunInfo, setPhase, turnDedupRef, reasoningSeenRef,
//         reasoningTimerRef, runIdRef, fetchMessages }
// fetchMessages: async () => raw[]  (GET session messages, reasoning 轮询数据源)
function buildTurnStreamHandler(deps) {
  const { setMessages, setRunInfo, setPhase, turnDedupRef,
          reasoningSeenRef, reasoningTimerRef, runIdRef, fetchMessages } = deps
  // 原子定位「最后一个 streaming 的 assistant 气泡」(无 index 竞态, 语义不变):
  // React 保证 updater 串行, 快速连续 delta 只作用在同一气泡上, 不碎片化。
  const findStreaming = (m) => {
    for (let i = m.length - 1; i >= 0; i--)
      if (m[i].role === 'assistant' && m[i].streaming) return i
    return -1
  }
  // 向 assistant 气泡追加字段。旧「穿插即关组」规则已删 (DESIGN §2.3):
  // 关组职责唯一归回合末的 closeTurnToolGroups。
  const appendField = (field, chunk) => setMessages((m) => {
    const i = findStreaming(m)
    if (i < 0) return [...m, { role: 'assistant', text: '', [field]: chunk, streaming: true, ts: Date.now() / 1000 }]
    const c = [...m]
    c[i] = { ...c[i], [field]: (c[i][field] || '') + chunk }
    return c
  })
  // reasoning 增量轮询 (DESIGN §2.4/§2.6): 每 1.5s GET messages 取真
  // reasoning/reasoning_content 增量, 渲染进 turnthinking 卡。
  // 去重双保险: per-message consumedLen 游标(reasoningSeenRef) + 片段级
  // thinking 集(turnDedupRef, 整段 trim 相等即跳过)。失败静默, 下 tick 重试。
  // REOPEN-FIX P1-1 (跨回合思考污染): GET messages 返回**全会话历史**,
  // 轮次边界必须显式锚定 —— run.started 时抓一次消息数作为本回合下界,
  // 轮询只处理下界之后的消息。下界未就绪(null)时轮询跳过, 回合末由
  // authoritativeReasoning(run.completed 只带本回合 transcript)权威收口,
  // 宁缺勿污。工厂每回合重建, 下界天然回合级隔离。
  let turnLowerBound = null
  const captureTurnLowerBound = async () => {
    try {
      const raw = await fetchMessages()
      turnLowerBound = Array.isArray(raw) ? raw.length : 0
    } catch { /* 下界取不到 → 本回合轮询全程跳过, 收口兜底, 不污染 */ }
  }
  const pollReasoning = async () => {
    if (turnLowerBound == null) return
    try {
      const raw = await fetchMessages()
      // 只取本回合下界之后的消息 (extract 内部本就跳过非 assistant,
      // 下界前历史轮 reasoning 不再进入本轮思考卡)。
      const turnMsgs = (Array.isArray(raw) ? raw : []).slice(turnLowerBound)
      const segs = extractReasoningSegments(turnMsgs, reasoningSeenRef.current)
      for (const seg of segs) {
        const key = seg.text.trim()
        const dd = turnDedupRef.current
        if (!key || (dd && dd.thinking.has(key))) continue
        if (dd) dd.thinking.add(key)
        appendTurnThinking(setMessages, seg.text)
      }
    } catch { /* 网络/权限失败静默, 回合末权威收口兜底 */ }
  }
  const handler = (ev, d) => {
    switch (ev) {
      case 'run.started':
        runIdRef.current = d.run_id || null
        setRunInfo({ status: 'running' })
        setPhase('thinking')
        // 回合开始: 重建去重指纹集(thinking 此后装已渲染的真 reasoning 片段)
        // + reasoning 消费游标, 并启动 1.5s reasoning 轮询
        turnDedupRef.current = { thinking: new Set(), msgIds: new Set() }
        reasoningSeenRef.current = new Map()
        clearInterval(reasoningTimerRef.current)
        // 锚定本回合下界 (P1-1): 异步抓, 完成前轮询 tick 自动跳过。
        captureTurnLowerBound()
        reasoningTimerRef.current = setInterval(pollReasoning, 1500)
        break
      case 'assistant.delta':
        if (d.delta) { appendField('text', d.delta); setPhase('responding') }
        break
      case 'assistant.commentary':
        // C-1: commentary 与已渲染 reasoning 同文时不二次渲染 (指纹集去重)
        if (d.text && !d.already_streamed) {
          const dd = turnDedupRef.current
          const key = d.text.trim()
          if (!key || !dd || !dd.thinking.has(key)) appendField('commentary', d.text)
        }
        break
      // 注: 旧 tool.progress(_thinking) 分支已整删 (DESIGN §2.4) —— 那是
      // _relay_thinking 的 500 字「可见正文预览」, 不是真 reasoning, 正是
      // issue #1 「思考过程显示最终回答」的根因。真思考走 pollReasoning。
      case 'tool.started':
        setRunInfo({ status: 'running', toolName: d.tool_name })
        setPhase('tool')
        pushToolCall(setMessages, d)
        break
      case 'tool.completed':
      case 'tool.failed':
        setRunInfo({ status: ev === 'tool.failed' ? 'tool_failed' : 'running', toolName: d.tool_name })
        setPhase('thinking')
        attachToolResult(setMessages, d, ev === 'tool.failed')
        break
      case 'assistant.completed':
        // C-2: 记录该回合 assistant 消息的 gateway id 进 msgIds ——
        // poll.recovered 重放同 id 消息时整条跳过 (AC1-2 主键去重)。
        if (d.message_id && turnDedupRef.current) {
          turnDedupRef.current.msgIds.add(d.message_id)
        }
        if (d.content) setMessages((m) => {
          const i = findStreaming(m)
          if (i < 0) return [...m, { role: 'assistant', text: d.content, streaming: true, ts: Date.now() / 1000 }]
          if ((m[i].text || '').trim()) return m
          const c = [...m]
          c[i] = { ...c[i], text: d.content }
          return c
        })
        break
      case 'run.completed':
      case 'run.failed':
      case 'run.cancelled':
        setRunInfo({ status: ev.replace('run.', '') })
        // 回合收口 (DESIGN §2.4): 停轮询 → 封组 → 权威覆盖思考卡 → 回填工具结果
        clearInterval(reasoningTimerRef.current)
        reasoningTimerRef.current = null
        closeTurnToolGroups(setMessages)
        sealTurnThinking(setMessages, authoritativeReasoning(d.messages))
        backfillToolResults(setMessages, d.messages)
        break
      case 'poll.broken':
        // SSE 连接异常断, chatStream 已自动切换到 fallback 轮询模式
        setPhase('recovering')  // 让用户看到状态
        break
      case 'poll.recovered':
        // chatStream fallback 抓到了一条 run 中断期间错过的消息
        // C-2 两级去重 (AC1-2): ① raw.id 已渲染过 → 整条跳过;
        // ② 半截气泡兜底: 流式气泡文本非空且持久化全文以其为前缀 →
        //   用全文替换气泡(不追加), 防"半截+全文"双份。
        {
          const raw = d.raw
          if (raw) {
            const dd = turnDedupRef.current
            if (!(raw.id != null && dd && dd.msgIds.has(raw.id))) {
              const ts = raw.timestamp || Date.now() / 1000
              const role = raw.role || 'tool'
              if (role === 'user' && raw.content) {
                setMessages((m) => [...m, { role: 'user', text: raw.content, ts }])
              } else if (role === 'assistant' && raw.content) {
                setMessages((m) => {
                  const i = findStreaming(m)
                  if (i >= 0) {
                    const bubble = (m[i].text || '').trim()
                    const copy = [...m]
                    if (bubble && raw.content.trim().startsWith(bubble)) {
                      // 持久化全文覆盖半截气泡, 结束流式
                      copy[i] = { ...copy[i], text: raw.content, streaming: false }
                    } else {
                      copy[i] = { ...copy[i], text: (copy[i].text || '') + raw.content, streaming: false }
                    }
                    return copy
                  }
                  return [...m, { role: 'assistant', text: raw.content, streaming: false, ts }]
                })
              }
              if (raw.id != null && dd) dd.msgIds.add(raw.id)
            }
            // tool role 不展示(噪音大)
          }
        }
        break
      default:
        break
    }
  }
  // 暴露下界捕获与轮询供回合起点锚定/测试驱动 (P1-1); handler 是唯一事件入口。
  handler.captureTurnLowerBound = captureTurnLowerBound
  handler.pollReasoning = pollReasoning
  return handler
}

// 流退出兜底 (DESIGN §2.4 finally): 清轮询 + 幂等封组 + 思考卡收口 + 气泡收尾。
// runStream / send 的 finally 共用, 防异常/abort 路径泄漏 timer 或留 streaming 残留。
function finalizeTurn(setMessages, timers) {
  clearInterval(timers.reasoning)
  clearInterval(timers.elapsed)
  closeTurnToolGroups(setMessages)
  sealTurnThinking(setMessages, '')
  setMessages((m) => {
    let i = -1
    for (let k = m.length - 1; k >= 0; k--)
      if (m[k].role === 'assistant' && m[k].streaming) { i = k; break }
    if (i < 0) return m
    const c = [...m]
    c[i] = { ...c[i], streaming: false }
    return c
  })
}


// 在 toolgroup 数组中找结果应挂载的条目 (DESIGN C-4, AC1-4):
// 优先同名(name)的最后一个 running 条目 (跨 toolgroup 统一扫);
// 同名无 running → 退化为整体最后一个 running 条目 (现状语义)。
// 无 tool_call_id 下的 name+FIFO 匹配; 完全同名并发是事件能力极限 (R-3)。
function findRunningToolItem(m, name) {
  for (let i = m.length - 1; i >= 0; i--) {
    if (m[i].role !== 'toolgroup') continue
    for (let j = m[i].items.length - 1; j >= 0; j--) {
      if (m[i].items[j].status === 'running' && m[i].items[j].name === name) return { gi: i, ij: j }
    }
  }
  for (let i = m.length - 1; i >= 0; i--) {
    if (m[i].role !== 'toolgroup') continue
    for (let j = m[i].items.length - 1; j >= 0; j--) {
      if (m[i].items[j].status === 'running') return { gi: i, ij: j }
    }
  }
  return null
}
function attachToolResult(setMessages, d, failed) {
  setMessages((m) => {
    const hit = findRunningToolItem(m, d.tool_name)
    if (!hit) return m
    const c = [...m]
    const raw = d.preview != null ? d.preview
      : (d.error != null ? d.error : null)
    let out = null
    if (raw != null) {
      if (typeof raw === 'string') out = raw
      else { try { out = JSON.stringify(raw, null, 2) } catch { out = String(raw) } }
    }
    // 不可变更新: 组对象与 items 数组都要复制, 防 React 浅比较漏渲染
    const grp = { ...c[hit.gi], items: [...c[hit.gi].items] }
    grp.items[hit.ij] = { ...grp.items[hit.ij],
      result: out != null ? out.slice(0, 4000) : null,
      status: failed ? 'error' : 'done' }
    c[hit.gi] = grp
    return c
  })
}

// 回合结束时用 run.completed 的 transcript(d.messages)回填工具结果。
// tool.completed 事件不带 result(gateway 只透传 {tool,duration,error}),
// 真正的执行输出在持久化的 role=tool 消息里, run.completed 一并带下来。
// 按调用顺序把每条 role=tool 的 content 填进 toolgroup 里未填结果的条目。
function backfillToolResults(setMessages, messages) {
  if (!Array.isArray(messages) || messages.length === 0) return
  const toolMsgs = messages.filter(m => m && m.role === 'tool' && m.content != null)
  if (toolMsgs.length === 0) return
  setMessages((m) => {
    const cards = []  // [{gi, ij}] 未填结果的条目 (遍历所有 toolgroup 的 items)
    for (let i = 0; i < m.length; i++) {
      if (m[i].role !== 'toolgroup') continue
      for (let j = 0; j < m[i].items.length; j++) {
        if (m[i].items[j].result == null) cards.push({ gi: i, ij: j })
      }
    }
    if (cards.length === 0) return m
    const c = [...m]
    // 从后往前配对: 最后一个未填结果的条目 ← 最后一条 tool 消息
    const n = Math.min(cards.length, toolMsgs.length)
    for (let k = 0; k < n; k++) {
      const { gi, ij } = cards[cards.length - 1 - k]
      const raw = toolMsgs[toolMsgs.length - 1 - k].content
      let out
      if (typeof raw === 'string') out = raw
      else { try { out = JSON.stringify(raw, null, 2) } catch { out = String(raw) } }
      // 不可变更新: 同 attachToolResult, 组对象与 items 数组逐层复制
      const grp = { ...c[gi], items: [...c[gi].items] }
      grp.items[ij] = { ...grp.items[ij], result: out.slice(0, 4000) }
      c[gi] = grp
    }
    return c
  })
}
// 用户气泡的展示文本(附件以文件名摘要呈现, 实际发送走 multimodal/路径)
function fullTextForDisplay(text, files) {
  if (!files || files.length === 0) return text
  const att = files.map(f => `📎 ${f.name}`).join(' ')
  return text ? `${text}\n${att}` : att
}

// 协作任务摘要卡解析 (CONSOLE2 §4.2, 红线 K1): 只读渲染层纯函数,
// 识别 Team Work 首条消息并抽取展示字段; 发送内容(firstMsg)零改动。
// 命中返回 {req, wf, ws}; 非命中返回 null, 渲染完全走原气泡。
// I18N-R3 (2026-10-16, REQ §3.4 #8/#16): 协议 marker 保持字面
// (en/zh 前端都通过 `startsWith`), 但 pick 字段冒号兼容中英 (en 生成
// 卡 + zh 前端互解; zh 生成卡 + en 前端互解)。
function parseDispatchCard(text) {
  if (!text || !text.trimStart().startsWith('【Team Work 多智能体任务】')) return null
  // I18N-R3: 字段冒号双键 (`需求:` / `Requirement:`, `工作目录:` / `Workspace:`,
  // `参考 workflow:` / `Reference workflow:`)。任一命中即视为该字段存在。
  const pickFirst = (prefixes) => {
    const lines = text.split('\n')
    for (const p of prefixes) {
      const line = lines.find((l) => l.trim().startsWith(p))
      if (line) return line.trim().slice(p.length).trim()
    }
    return ''
  }
  // 需求: 中英键都试
  let req = pickFirst(['需求:', 'Requirement:'])
  if (req.length > 120) req = req.slice(0, 120) + '…'
  // 协作流程: 中英键都试;auto 时显示「自动选择」(i18next 化的标签)
  let wf = pickFirst(['参考 workflow:', 'Reference workflow:'])
  if (!wf || wf.includes('自行') || wf.toLowerCase().includes('yourself')
      || wf.toLowerCase().includes('auto')) wf = '__AUTO_SELECT__'  // 占位渲染前
  // 工作目录: 中英键都试, 去掉路径前缀与尾注
  let ws = pickFirst(['工作目录:', 'Workspace:'])
  // 中英分隔符统一支持 (zh `——`, en ` — `)
  ws = ws.split(/——| — /)[0].trim()
  if (ws && ws !== '未指定' && ws.toLowerCase() !== 'not specified') {
    ws = ws.split('/').filter(Boolean).pop() || ws
  }
  return { req, wf, ws }
}

// 消息模型: { role: 'user'|'assistant'|'tool'|'toolgroup'|'turnthinking', text, streaming?, toolName?, commentary? }

// / 命令表: 纯前端拦截, 映射到已有能力, 不新增后端协议。
// [en] Slash command descriptions live in i18next so the help popup renders
//      in the active language; cmd token stays Latin (machine protocol).
// [zh-cn] / 命令的 desc 走 i18next;cmd token 仍是协议拉丁键。
function buildSlashCommands(t) {
  return [
    { cmd: '/steer',    desc: t('chat.commands.steer_desc') },
    { cmd: '/new',      desc: t('chat.commands.new_desc') },
    { cmd: '/sessions', desc: t('chat.commands.sessions_desc') },
    { cmd: '/clear',    desc: t('chat.commands.clear_desc') },
    { cmd: '/status',   desc: t('chat.commands.status_desc') },
    { cmd: '/help',     desc: t('chat.commands.help_desc') },
  ]
}

// [en] ChatView exposes `openTeamWork` via useImperativeHandle so the global
//      「核心功能」/Core Features button in App.jsx can trigger the modal
//      from any view (R2.3 — cross-component trigger, ChatView's internal
//      logic untouched). The exposed handle is intentionally minimal: just
//      openTeamWork. All state/transitions (twTask/twWorkspace reset,
//      workflow list fetch, etc.) live inside ChatView's own openTeamWork.
// [zh-cn] ChatView 通过 useImperativeHandle 暴露 openTeamWork,让 App.jsx
//        的全局「核心功能」按钮可从任意视图触发弹窗(R2.3 跨组件触发,
//        ChatView 内部逻辑零改动)。暴露 handle 仅含 openTeamWork,
//        twTask/twWorkspace 重置/拉 workflow 列表等全部由 ChatView 内部
//        openTeamWork 承担。
const ChatView = React.forwardRef(function ChatView(
  { base = '', targetLabel = 'Main Agent', onClose = null, active = false }, ref) {
  // base='' → main; base='/api/w/<worker>' → 指定 worker(经 console 代理)
  const { t } = useTranslation()
  const SLASH_COMMANDS = buildSlashCommands(t)
  const g = React.useMemo(() => makeGw(base), [base])
  const sidKey = `sas-chat-sid${base ? ':' + base : ''}`
  const [sessionId, setSessionId] = useState(() => localStorage.getItem(sidKey) || '')
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [booting, setBooting] = useState(true)
  const [sessions, setSessions] = useState([])
  const [listOpen, setListOpen] = useState(false)
  const [loadingHist, setLoadingHist] = useState(false)
  const [slashIdx, setSlashIdx] = useState(0)  // / 命令补全选中项
  const [pendingFiles, setPendingFiles] = useState([])  // {name, path, bytes}
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)
  const [runInfo, setRunInfo] = useState(null)  // {status, toolName}
  const [phase, setPhase] = useState(null)      // 'thinking' | 'tool' | 'responding' | 'steering'
  const [elapsed, setElapsed] = useState(0)    // 本轮已处理秒数
  const elapsedTimerRef = useRef(null)
  const abortRef = useRef(null)
  const runIdRef = useRef(null)   // 当前运行中的 run_id, steer 用
  // Issue#8 (2026-10-13): 轮询守门用的 ref —— 即使 React 18 strict mode
  // 把 setSessionId 与 setInterval 排在不同宏任务, 旧 tick 也能在 await
  // 后比对「closure sid vs 当前 React sid」, 不等则丢结果。这个 ref 在
  // useEffect 入口同步写; 与 cancelled 互为双层保险。
  const currentSessionIdRef = useRef(null)
  // 回合级去重指纹 (DESIGN C-1/C-2 + §2.6): run.started 时重建。
  // thinking: Set of 已渲染的真 reasoning 片段(trim 后) —— commentary 与
  //           已渲染 reasoning 同文时不二次渲染;
  // msgIds:   Set of 已渲染消息的 gateway id(assistant.completed message_id +
  //           poll.recovered raw.id), 防 SSE fallback 二次追加。
  const turnDedupRef = useRef(null)
  // reasoning 轮询消费游标 (DESIGN D8): Map<msgKey, consumedLen>, 回合开始重建;
  // 与 poll.recovered 的 msgIds 分离各读各游标, 互不重复追加。
  const reasoningSeenRef = useRef(new Map())
  const reasoningTimerRef = useRef(null)  // 1.5s reasoning 轮询 timer (回合内活, finally 清)

  const scrollRef = useRef(null)
  const gwStatusRef = useRef(null)
  // issue #75 B2 §2: 首次激活是否已拉过历史 (幂等闸门; 切回 tab 不重复拉)
  const didLoadHistRef = useRef(false)
  // issue #75 B3 §5.3/§5.4: 历史分页游标 + 去重 Set + 「更多」加载态
  const histMetaRef = useRef({ hasMore: false, beforeCursor: null })
  const seenIdsRef = useRef(new Set())   // gateway 消息 id 幂等集 (§5.3 去重)
  const [histMore, setHistMore] = useState(false)      // 「更多」按钮显隐
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreErr, setMoreErr] = useState('')
  // ── Team Work 启动器状态 ──
  const [twOpen, setTwOpen] = useState(false)           // 弹窗开关
  const [twTask, setTwTask] = useState('')              // 任务需求文本
  const [twWorkflow, setTwWorkflow] = useState('auto')  // 选中的 workflow
  const [twWorkflows, setTwWorkflows] = useState(null)  // 从 /api/workflows 拉到的列表
  const [twLaunching, setTwLaunching] = useState(false) // 提交中
  const [twError, setTwError] = useState('')            // 弹窗内错误
  const [twWorkspace, setTwWorkspace] = useState('')    // 弹窗里选的工作目录(空=继承 session 绑定)
  // ── 工作目录(workspace)选择器状态 ──
  const [workspaces, setWorkspaces] = useState([])       // [{name,path,writable,mtime}]
  const [wsRoot, setWsRoot] = useState('/share_workspace/projects')
  const [wsOpen, setWsOpen] = useState(false)           // 下拉开关
  const [wsNewName, setWsNewName] = useState('')        // 新建目录名
  const [wsBusy, setWsBusy] = useState(false)
  const [wsCreateError, setWsCreateError] = useState('')  // 新建目录错误就近显示 (§4.7)
  const [histLoaded, setHistLoaded] = useState(false)     // 首屏历史加载完成标记 (§4.5 骨架屏)
  // issue #75 s10 C-1: 首拉失败态 → 可重试闭环。
  // histLoadFailed 决定错误条显隐; histErr 存失败原因(供错误文案插值);
  // histLoading 屏蔽并发重试(防重复拉取); histRetryTick 是 Retry 的重拉
  // 触发器 (进 effect 依赖 → 用 state 不用 ref, 保证 effect 一定重跑)。
  const [histLoadFailed, setHistLoadFailed] = useState(false)
  const [histErr, setHistErr] = useState('')
  const [histLoading, setHistLoading] = useState(false)
  const [histRetryTick, setHistRetryTick] = useState(0)
  // issue #75 s10 C-1 竞态三件套 (ref 语义, 不进渲染):
  //   histFetchInFlightRef — 是否有历史请求在飞 (防重复拉取; state 有延迟,
  //                          连续两次点击 Retry 必须在同一帧内也只发一次);
  //   histPendingSidRef   — 失败待重试的会话 sid (空串 = 无待重试);
  //                          来源: 首拉 effect 失败 / switchSession 失败;
  //   histCurSidRef       — 当前 React 会话 sid, 用于「过期响应丢弃」判定
  //                          (重试在飞行中切会话 → 旧响应不得覆盖新会话画面)。
  const histFetchInFlightRef = useRef(false)
  const histPendingSidRef = useRef('')
  // 初值取当前 sessionId (不是空串): 让 ref 初值与真源一致, 不依赖
  // 「同步 sid 到 ref 的 effect 一定先于首拉响应到达」这一时序假设。
  // (实测: React 的 passive effect 在同一 commit 内按声明顺序同步冲刷,
  //  该时序当前成立; 但用初值对齐而非依赖时序, 语义更稳, 且当未来把
  //  同步 effect 挪位/合并时不会静默退化成「首拉响应被误判为过期丢弃」。)
  const histCurSidRef = useRef(sessionId)
  // Issue #46 (s4-impl): 文件浏览器抽屉开合 + 聊天链接直达目标
  const [fbOpen, setFbOpen] = useState(false)
  const [fbReveal, setFbReveal] = useState(null)
  // 每个 session 绑定一个 workspace: sas-chat-ws:<sid> = <path>
  const wsKeyFor = (sid) => `sas-chat-ws${base ? ':' + base : ''}:${sid || 'draft'}`
  const [workspace, setWorkspace] = useState(() =>
    localStorage.getItem(wsKeyFor(localStorage.getItem(sidKey))) || '')

  // 历史窗口加载 (issue #75 B3 §5.2/§5.3): before=null 首屏取最近一批;
  // before='o:<n>' 前插更早批次 + 滚动锚定 (INV-08 视口不前跳)。
  // 后端回退兼容: 无 history 字段 (旧后端) → hasMore=false, 条目走
  // windowedToMessages 的旧全量分支。
  const loadHistoryWindow = async (sid, before) => {
    const d = await g.getMessages(sid, { want: HIST_WANT, before })
    const meta = (d && d.history) || {}
    histMetaRef.current = { hasMore: !!meta.has_more,
                            beforeCursor: meta.before_cursor || null }
    setHistMore(!!meta.has_more)
    const raw = d.data || []
    const seen = seenIdsRef.current
    // issue #75 s9 B-1 (前端兜底, 双保险): 续翻批按 id 幂等集过滤, 任何与
    // 已展示批次重叠的条目都不再渲染 (后端已修 limit 越界, 此处再兜一层)。
    // alreadySeen 快照必须在把本批 id 写进 seen 【之前】取 —— 否则本批
    // 自身会被整批滤掉。id 为 null 的条目无法判重, 保留 (与后端一致)。
    const alreadySeen = new Set(seen)
    for (const e of raw) if (e && e.id != null) seen.add(e.id)
    if (!before) {
      setMessages(windowedToMessages(raw))
      return
    }
    // 前插批: 只追加未见过的条目 (重叠条目整条丢弃, 不产生半条重复)
    const freshRaw = raw.filter(
      (e) => !(e && e.id != null && alreadySeen.has(e.id)))
    const prependMsgs = windowedToMessages(freshRaw)
    // 前插 + 滚动锚定: 记旧 scrollHeight/scrollTop, 提交后补差值 (§5.3)
    const el = scrollRef.current
    const oldH = el ? el.scrollHeight : 0
    const oldTop = el ? el.scrollTop : 0
    setMessages((m) => [...prependMsgs, ...m])
    requestAnimationFrame(() => {
      const el2 = scrollRef.current
      if (el2) el2.scrollTop = oldTop + (el2.scrollHeight - oldH)
    })
  }

  // 「更多历史对话」按钮 (§3 四态: 加载态 disabled+.spin / 错误态内联)
  const loadMoreHistory = async () => {
    if (loadingMore || !sessionId || !histMetaRef.current.hasMore) return
    setLoadingMore(true)
    setMoreErr('')
    try {
      await loadHistoryWindow(sessionId, histMetaRef.current.beforeCursor)
    } catch (e) {
      setMoreErr(String(e.message || e))
    } finally {
      setLoadingMore(false)
    }
  }

  // 启动: 探测后端代理就绪(旧版 console 无端点时提示升级)
  // worker 对话(base 非空)跳过 main 专属 gwBootstrap, 直接可用
  // issue #75 B2 §2: 加 active 闸门 — 非激活 tab 零 fetch (AC-14);
  // 恒挂载不卸载 (INV-01: 切走保留 state), 首次激活才跑。
  useEffect(() => {
    if (base) { setBooting(false); return }
    if (!active) return
    (async () => {
      const d = await gwBootstrap()
      gwStatusRef.current = d
      setBooting(false)
      if (d && d.enabled === false) {
        setError(t('chat.errors.gateway_disabled'))
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  // 恢复上次会话的历史(刷新后不丢画面)
  // issue #75 B2 §2: active 闸门 + didLoadHistRef 幂等 — 首次激活拉一次,
  // 切回不重复拉 (INV-01/INV-02: state 保留, 历史已在)。
  useEffect(() => {
    if (!active) return
    if (!sessionId) { setHistLoaded(true); return }
    if (didLoadHistRef.current) return
    if (histFetchInFlightRef.current) return   // 已有请求在飞 → 不重复发
    didLoadHistRef.current = true
    histFetchInFlightRef.current = true
    setHistLoading(true)
    // issue #75 s10 D-1 (P0 白屏崩溃): 赋值语句后换行接 '(' 时 ASI 不生效,
    // 解析成 `true(async()=>{...})()` → TypeError: true is not a function →
    // React 树卸载 → 整页白屏。用 `void` 前缀显式丢弃 IIFE 返回值, 让这行
    // 不再依赖 ASI 的插入点判定 (行首 '(' 是 ASI 危险首字符)。
    void (async () => {
      const sid = sessionId           // 捕获本次请求的 sid (过期响应判定用)
      try {
        await loadHistoryWindow(sid, null)
        // C-1: 首拉成功才闸门保持 true; 失败回滚 false, 下次激活可重试。
        // 过期响应 (期间已切到别的会话) → 丢弃, 不覆盖新会话画面。
        if (histCurSidRef.current !== sid) return
        setHistLoadFailed(false)
        setHistErr('')
        histPendingSidRef.current = ''
      } catch (e) {
        if (histCurSidRef.current !== sid) return   // 过期失败不弹错误条
        // C-1 (P2): 首拉失败不再静默吞 —— 报错条展示 + 回滚闸门, 切走再回
        // 或点 Retry 都能重新拉; 失败时保持空列表 (不渲染半截)。
        setHistLoadFailed(true)
        setHistErr(String(e?.message || e))
        histPendingSidRef.current = sid          // 记下待重试的会话
        didLoadHistRef.current = false           // 回滚幂等闸门 → 下次激活自动重试
      }
      finally {
        histFetchInFlightRef.current = false     // 解锁 (失败/成功都解锁)
        if (histCurSidRef.current === sid) {
          setHistLoaded(true)   // then/catch 均算完成, 骨架屏退场 (§4.5)
          setHistLoading(false)  // 解除重试屏蔽 (失败/成功都解锁)
        }
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, sessionId, histRetryTick])  // histRetryTick: Retry 按钮重拉触发器

  // 同步当前会话 sid 到 ref (过期响应判定 + 待重试会话判定共用同一真源)。
  useEffect(() => { histCurSidRef.current = sessionId }, [sessionId])

  // C-1 (P2) 显式重试入口: 错误条上的「重试」按钮。
  // 覆盖两种失败来源:
  //   (A) 当前会话首拉失败 (histPendingSidRef === sessionId) → 解除幂等闸门
  //       + bump tick → effect 重跑一次首拉;
  //   (B) 切会话失败 (histPendingSidRef !== sessionId, 指向目标 sid) →
  //       重放这次切换 (走 switchSession 同一路径)。
  // 竞态纪律: (1) 重试期间 histFetchInFlightRef=true → 按钮 disabled, 不
  //           重复发请求 (同帧连点两次也只发一次);
  //           (2) 闸门 didLoadHistRef 在失败时已回滚为 false → 本函数先复位
  //               为 false (解除幂等闸门), 再 bump tick 让 effect 重跑一次
  //               真正的拉取; 顺序不能反 (先 bump 会被 effect 入口的
  //               `if (didLoadHistRef.current) return` 直接跳过);
  //           (3) 切走 (active=false) 时 effect 直接 return, 不发请求;
  //               重试请求在飞行中切走 → 响应按 sid 判定过期后丢弃
  //               (histCurSidRef 比对), 不会覆盖新会话画面;
  //           (4) 无待重试会话 → 不重拉 (不产生野请求)。
  const retryHistoryLoad = () => {
    if (histFetchInFlightRef.current || histLoading) return
    const pending = histPendingSidRef.current
    if (!pending) return
    if (pending !== sessionId) {
      // (B) 切会话失败 → 重放切换 (sessionId 未变, 故切走判断靠 sid 比对)
      if (!active) return
      void switchSession(pending)
      return
    }
    // (A) 当前会话首拉失败 → 解除幂等闸门 + bump tick 让 effect 重跑
    didLoadHistRef.current = false   // 解除幂等闸门 → 允许 effect 再跑一次
    setHistLoadFailed(false)        // 先清错误条, 重拉成功后再按结果置位
    setHistErr('')
    setHistRetryTick((n) => n + 1)   // 触发 effect 重跑 (state 变更 → 依赖变化)
  }

  // ★ 新增: 轮询拉新消息, 修复"不刷新页面看不到新对话内容"问题。
  // gateway 没有 session-level SSE(/api/sessions/{id}/chat/stream 是 run 级,
  //  run 结束就断), 后端 /api/events 推的是 task 事件不是 chat 消息。
  // 唯一稳定通道: 周期性 GET /api/sessions/{id}/messages, 比对消息数追加。
  // - 只在有 session 且当前没在跑 run 时拉(避免双流冲突)
  // - 检测到新消息 → setMessages 追加; 无新消息 → 不触发 rerender
  // - 间隔 4s, 平衡实时性和 server 负载
  // Issue#8 (2026-10-13): 加双层保险防轮询串扰。React `useEffect` 在
  // sessionId 变化时重建 interval + 取消旧闭包是 hot-path 正确做法
  // (cleanup 设 cancelled, 后置 await 守门挡掉旧 callback 写入)。
  // 但若 (1) React 18 自动批处理把 setInterval/tick 推迟, 或 (2) 旧 tick
  // 已经在 await 中, await resolve 后只检查 cancelled 还不够 — 因为
  // setMessages 是基于已 closure 捕获的 sid 决定的, 而那时 React state
  // 已换, 网络结果会写错会话的 messages。再加一层: 把 closure 里的 sid
  // 与 React 当前 sid 比对, 不等就丢。
  useEffect(() => {
    // issue #75 B2/B3: active 闸门 (非激活 tab 不创建 interval, AC-14);
    // §5.4-1: lastCount 在窗口化后失效 → 增量判定改 id 水位。
    if (!active || !sessionId) return
    let cancelled = false
    const seen = seenIdsRef.current

    const tick = async () => {
      if (cancelled) return
      // 忙时跳过: chatStream 当前 run 正在写消息, 拉会和它打架
      if (busy) return
      try {
        const d = await g.getMessages(sessionId, { want: HIST_WANT })
        // ★ Issue#8: 双层保险之一 —— await 期间 React 可能已切换 session,
        // 这时若不守门, 旧 tick 会把网络结果塞进新 sid 的 messages,
        // 引发跨会话串扰 (issue #8 截图复现形态)。
        if (cancelled) return
        // ★ Issue#8: 双层保险之二 —— 即使 cancelled 假阳性过, 还要
        // 确认 closure 捕获的 sid 等于当前最新 React sid (React 18
        // strict mode / 高并发下可能把 setInterval/tick 排到下次宏任务,
        // 此时 React 已批处理过 setSessionId, 旧 tick 读 sid 已经不是
        // 最新的了 —— 实测这是偶发漂移根因)。
        if (sessionId !== currentSessionIdRef.current) return
        const raw = d.data || []
        // id 水位增量 (§5.4-1): 只追加从未见过的 id (窗口头部之外新增的
        // 尾部消息); 已见过的 (窗口滑动覆盖 / 重复) 跳过。
        const freshRaw = raw.filter((e) => e && e.id != null && !seen.has(e.id))
        if (freshRaw.length) {
          for (const e of freshRaw) seen.add(e.id)
          const fresh = windowedToMessages(freshRaw)
          if (fresh.length) {
            setMessages((m) => [...m, ...fresh])
          }
        }
      } catch { /* 网络/权限问题静默, 下轮再试 */ }
    }
    // 把当前 sid 同步到 ref, 供 tick 在每次 await 后即时比对。
    currentSessionIdRef.current = sessionId
    const id = setInterval(tick, 4000)
    return () => { cancelled = true; clearInterval(id) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, sessionId, busy])  // 激活/会话/busy 翻转时重建 interval

  useEffect(() => {
    if (sessionId) localStorage.setItem(sidKey, sessionId)
  }, [sessionId])

  // 拉工作目录清单(issue #75 B2: 仅激活时拉; 打开选择器时再拉一次保证新鲜)
  const loadWorkspaces = useCallback(async () => {
    try {
      const d = await api.workspaces()
      setWorkspaces(d.workspaces || [])
      setWsRoot(d.root || '/share_workspace/projects')
    } catch { /* 后端未就绪, 保持空列表 */ }
  }, [])
  useEffect(() => { if (active) loadWorkspaces() }, [active, loadWorkspaces])

  // session 切换 → 恢复该 session 绑定的 workspace(新会话无绑定则空)
  useEffect(() => {
    setWorkspace(localStorage.getItem(wsKeyFor(sessionId)) || '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // 选定 workspace: 立即持久化到当前 session(或 draft)的 key
  const pickWorkspace = (path) => {
    setWorkspace(path)
    localStorage.setItem(wsKeyFor(sessionId), path)
    setWsOpen(false)
    setWsCreateError('')  // 面板关闭时清除新建错误 (§4.7)
  }

  // 新 session 创建后, 把 draft 期选的 workspace 迁到新 sid 名下
  // (ensureSession 内联了同样逻辑; Team Work 直接 createSession 时用它)
  const migrateDraftWorkspace = (newSid) => {
    const draftWs = localStorage.getItem(wsKeyFor(''))
    if (draftWs) {
      localStorage.setItem(wsKeyFor(newSid), draftWs)
      localStorage.removeItem(wsKeyFor(''))
      return draftWs
    }
    return localStorage.getItem(wsKeyFor(newSid)) || ''
  }

  // 新建工作目录(严格白名单在后端校验)
  // CONSOLE2 §4.7: 失败写 wsCreateError 就近显示, 不再污染全局 error 通道
  const createWorkspace = async () => {
    const name = wsNewName.trim()
    if (!name || wsBusy) return
    setWsBusy(true)
    setWsCreateError('')
    try {
      const d = await api.workspaceCreate(name)
      await loadWorkspaces()
      pickWorkspace(d.path)
      setWsNewName('')
    } catch (e) {
      setWsCreateError(t('chat.errors.new_folder_failed', { err: String(e.message || e) }))
    } finally {
      setWsBusy(false)
    }
  }

  // 自动滚底 — issue #75 §5.3/INV-08: 只在「用户贴底 (距底<40px)」或
  // 新消息从底部追加时滚; 前插历史 (用户在中上部) 不把视口拽到底。
  // stickBottomRef 在滚动时实时更新; 发消息/流式更新走 append 路径,
  // 用户此时必在底部附近 → 照常滚。
  const stickBottomRef = useRef(true)
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      stickBottomRef.current =
        el.scrollHeight - el.scrollTop - el.clientHeight < 40
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])
  useEffect(() => {
    const el = scrollRef.current
    if (el && stickBottomRef.current) el.scrollTop = el.scrollHeight
  }, [messages, runInfo])

  // Issue #46 (s4-impl): 聊天消息中本地文件路径 → data-fb-path 链接增强。
  // 每次 messages 渲染后跑一次,幂等,失败静默(增强失败不阻塞聊天渲染)。
  useEffect(() => {
    const el = scrollRef.current
    if (!el || !workspace) return
    try {
      enhanceFileLinks(el, workspace, (abs) => {
        // 相对路径已由 enhanceFileLinks 转绝对,直接走 reveal 流程
        const rel = abs.slice(workspace.length).replace(/^\/+/, '')
        setFbOpen(true)
        setFbReveal(rel)
      })
    } catch { /* 增强失败不阻塞聊天 */ }
  }, [messages, workspace])

  const ensureSession = useCallback(async () => {
    if (sessionId) return sessionId
    const sid = await g.createSession()
    setSessionId(sid)
    // 新 session 创建后, 把 draft 期选的 workspace 迁到新 sid 名下
    const draftWs = localStorage.getItem(wsKeyFor(''))
    if (draftWs) {
      localStorage.setItem(wsKeyFor(sid), draftWs)
      localStorage.removeItem(wsKeyFor(''))
    }
    return sid
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // 可复用流式执行: 把 content 发给指定(或当前)session, 流式渲染 assistant 回复。
  // sidOverride 用于 Team Work 刚 createSession 后 React state 尚未更新的场景。
  // 事件处理全部委托 buildTurnStreamHandler 公共工厂 (DESIGN D6), 与 send 共用。
  const runStream = useCallback(async (content, sidOverride) => {
    setBusy(true)
    setPhase('queued')
    setElapsed(0)
    clearInterval(elapsedTimerRef.current)
    elapsedTimerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000)
    try {
      const sid = sidOverride || await ensureSession()
      const ac = new AbortController()
      abortRef.current = ac
      const handler = buildTurnStreamHandler({
        setMessages, setRunInfo, setPhase, turnDedupRef, reasoningSeenRef,
        reasoningTimerRef, runIdRef,
        fetchMessages: async () => ((await g.getMessages(sid)) || {}).data || [],
      })
      await g.chatStream(sid, content, handler, ac.signal)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      finalizeTurn(setMessages, { reasoning: reasoningTimerRef.current, elapsed: elapsedTimerRef.current })
      reasoningTimerRef.current = null
      setBusy(false)
      setPhase(null)
      setRunInfo(null)
      runIdRef.current = null
      abortRef.current = null
    }
  }, [ensureSession])


  const send = useCallback(async () => {
    const text = input.trim()
    if (!text && pendingFiles.length === 0) return

    // ★ 处理中收到新消息 → steer: 注入当前运行中的 run(Hermes 原生), 不打断执行
    if (busy) {
      // 剥掉显式输入的 /steer 前缀
      const steerText = text.replace(/^\/steer\s*/i, '').trim()
      // issue#4 (P1): 转向消息也透传 session 绑定工作目录 — main 中途
      // 注入的指令同样需要知道当前任务在哪个目录工作, 否则后续 worker
      // 产出落错目录。
      const wsLine = workspace ? t('chat.input.ws_line_session', { ws: workspace }) : ''
      if (!steerText) { setError(t('chat.errors.steer_empty')); return }
      const rid = runIdRef.current
      if (!rid) { setError(t('chat.errors.no_active_run_to_steer')); return }
      if (pendingFiles.length > 0) { setError(t('chat.errors.steer_no_attachment')); return }
      setInput('')
      setError('')
      setMessages((m) => [...m, { role: 'user', text: `/steer ${steerText}`, steer: true, ts: Date.now() / 1000 }])
      try {
        await g.steer(rid, steerText + wsLine)
        setMessages((m) => [...m, { role: 'tool', text: t('chat.errors.steer_injected'), ts: Date.now() / 1000 }])
        setPhase('steering')   // 原 SSE 流仍在, 后续事件会自动覆盖阶段
      } catch (e) {
        setError(t('chat.errors.steer_failed', { err: String(e.message || e) }))
      }
      return
    }

    setInput('')
    setError('')

    // / 命令拦截: 本地执行, 不进对话
    if (text.startsWith('/')) {
      const cmd = text.split(/\s+/)[0].toLowerCase()
      setMessages((m) => [...m, { role: 'user', text, ts: Date.now() / 1000 }])
      const note = (msg) => setMessages((m) => [...m, { role: 'tool', text: msg, ts: Date.now() / 1000 }])
      if (cmd === '/new') { newChat(); note(t('chat.errors.session_started')) }
      else if (cmd === '/steer') { note(t('chat.errors.idle_no_steer')) }
      else if (cmd === '/sessions') { openList() }
      else if (cmd === '/clear') { setMessages([]) }
      else if (cmd === '/status') {
        const d = gwStatusRef.current
        if (d) {
          const ena = d.enabled ? t('chat.gwconfig.enabled') : t('chat.gwconfig.disabled')
          const prx = d.proxy ? t('chat.gwconfig.proxy_ready') : t('chat.gwconfig.unavailable')
          note(`Gateway: ${ena} · ${prx}`)
        } else { note(t('chat.gwconfig.unknown')) }
      }
      else if (cmd === '/help') {
        note(t('chat.commands.label') + '\n' + SLASH_COMMANDS.map(c => `${c.cmd}  —  ${c.desc}`).join('\n'))
      }
      else { note(t('chat.commands.unknown', { cmd })) }
      return
    }

    // 附件: 图片走 Hermes 原生 multimodal parts(data:image), agent 直接"看见";
    // 非图片文件走共享卷路径 + 明确 read_file 指令(有限增强)。
    const files = pendingFiles
    let content
    // issue#4 (P1): 聊天消息注入 session 绑定工作目录。
    // 当前会话已绑定 workspace (顶栏选择器/Team Work 启动器持久化到
    // sas-chat-ws:<sid>) 时, 在发送给 main 的正文末尾追加一行「工作目录: <path>」,
    // 让 main 明确本会话任务产出落哪个目录, 不再靠猜。
    // 用户可见气泡保留原始输入(不重复展示), 只有实际发送的 content 被注入。
    const wsLine = workspace ? t('chat.input.ws_line_session', { ws: workspace }) : ''
    if (files.length) {
      const parts = []
      if (text) parts.push({ type: 'text', text })
      for (const f of files) {
        if (f.isImage) {
          parts.push({ type: 'image_url', image_url: { url: f.dataUrl } })
        } else {
          parts.push({ type: 'text',
            text: t('chat.input.attachment_placeholder_msg', { name: f.name, path: f.path }) })
        }
      }
      content = parts
      // 多模态 parts: 把工作目录提示并入首个 text part, 保持结构完整
      if (wsLine) {
        if (parts.length > 0 && parts[0].type === 'text') {
          parts[0] = { ...parts[0], text: parts[0].text + wsLine }
        } else {
          parts.unshift({ type: 'text', text: wsLine.trimStart() })
        }
        content = parts
      }
    } else {
      content = text + wsLine
    }
    setPendingFiles([])

    setBusy(true)
    setPhase('queued')
    setElapsed(0)
    clearInterval(elapsedTimerRef.current)
    elapsedTimerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000)
    setMessages((m) => [...m, { role: 'user', text: fullTextForDisplay(text, files), files, ts: Date.now() / 1000 }])
    try {
      const sid = await ensureSession()
      const ac = new AbortController()
      abortRef.current = ac
      // 事件处理与 runStream 共用公共工厂 (DESIGN D6), 不再维护两套 switch
      const handler = buildTurnStreamHandler({
        setMessages, setRunInfo, setPhase, turnDedupRef, reasoningSeenRef,
        reasoningTimerRef, runIdRef,
        fetchMessages: async () => ((await g.getMessages(sid)) || {}).data || [],
      })
      await g.chatStream(sid, content, handler, ac.signal)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      finalizeTurn(setMessages, { reasoning: reasoningTimerRef.current, elapsed: elapsedTimerRef.current })
      reasoningTimerRef.current = null
      setBusy(false)
      setPhase(null)
      setRunInfo(null)
      runIdRef.current = null
      abortRef.current = null
    }
  }, [input, busy, ensureSession, pendingFiles])


  const uploadFiles = async (fileList) => {
    if (!fileList || fileList.length === 0) return
    setUploading(true)
    setError('')
    try {
      for (const f of Array.from(fileList)) {
        const dataUrl = await new Promise((resolve, reject) => {
          const r = new FileReader()
          r.onload = () => resolve(r.result)
          r.onerror = () => reject(new Error(t('chat.errors.file_read_failed')))
          r.readAsDataURL(f)
        })
        const isImage = (f.type || '').startsWith('image/')
        if (isImage) {
          // 图片: 不落盘, 直接以 data:image 走 Hermes 原生 multimodal
          setPendingFiles((p) => [...p, { name: f.name, isImage: true, dataUrl, bytes: f.size }])
          continue
        }
        const res = await fetch('/api/upload', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: f.name, data: dataUrl }),
        })
        const d = await res.json().catch(() => ({}))
        if (!res.ok) throw new Error(d.error || t('chat.errors.upload_failed', { status: res.status }))
        setPendingFiles((p) => [...p, { name: d.name, path: d.path, bytes: d.bytes }])
      }
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  // issue #63: 显式「停止」= 先经原生 POST /v1/runs/{run_id}/stop 真中断回合,
  // 再 abort 本地 fetch。代理保活落地后仅 abort 会被排空接管(回合继续跑完),
  // 用户主动停的语义会失效 —— run_id 已在 run.started 事件捕获(runIdRef)。
  // stop 失败不阻断 abort(至少让用户侧流断开); 仅显式点击走本函数,
  // 切会话/卸载路径(上方 abortRef abort)只 abort 不 stop, 由保活接管。
  const stop = async () => {
    const rid = runIdRef.current
    if (rid) {
      try {
        await g.stop(rid)
      } catch (e) {
        setError(t('chat.errors.stop_failed', { err: String(e.message || e) }))
      }
    }
    abortRef.current?.abort()
  }

  const newChat = () => {
    if (busy) return
    setSessionId('')
    setMessages([])
    setError('')
    // issue #75 B3: 新会话 = 无历史, 重置分页游标/去重集/「更多」态
    didLoadHistRef.current = true  // 空会话无需再拉历史窗口
    histMetaRef.current = { hasMore: false, beforeCursor: null }
    seenIdsRef.current = new Set()
    setHistMore(false)
    setMoreErr('')
    // issue #75 s10 C-1: 新会话 = 旧的失败态与待重试标记一并归位
    // (否则上一会话的加载失败错误条会挂在空白新会话上, 且 Retry 会去
    //  重拉一个已经放弃的旧会话)。
    setHistLoadFailed(false)
    setHistErr('')
    histPendingSidRef.current = ''
    setListOpen(false)
    localStorage.removeItem(sidKey)
  }

  const openList = async () => {
    if (listOpen) { setListOpen(false); return }
    setListOpen(true)
    try {
      const d = await g.listSessions(30)
      setSessions((d.data || []).filter(s => !s.archived && !s.hidden))
    } catch (e) { setError(String(e.message || e)) }
  }

  // ── Team Work 启动器 ──
  const openTeamWork = async () => {
    setTwError('')
    setTwTask('')
    setTwWorkflow('auto')
    setTwWorkspace(workspace || '')  // 默认继承当前 session 的工作目录
    setTwOpen(true)
    // 拉 workflow 列表(后端未就绪时 fallback 到 auto-only)
    try {
      const d = await api.workflows()
      setTwWorkflows(d.workflows || null)
    } catch (e) {
      setTwWorkflows(null)
    }
    loadWorkspaces()  // 刷新工作目录清单
  }
  const closeTeamWork = () => {
    if (twLaunching) return
    setTwOpen(false)
    setTwError('')
  }
  // 把 openTeamWork 暴露给父组件(App.jsx 的全局「核心功能」按钮使用)。
  // R2.6: 全局按钮恒可点击 —— 这里不感知/不调用 busy,流式回复中点击
  //       弹窗仍会打开,真正的旧流清理交给 submitTeamWork 内部的 (a) 步
  //       abort 守卫(issue#8/12 已固)。若被打开弹窗=已开状态,内部 set
  //       函数仍是幂等(workflows 重新拉、workspace 重新 inherit)。
  React.useImperativeHandle(ref, () => ({
    openTeamWork,
  }), [openTeamWork])
  const submitTeamWork = async () => {
    const task = twTask.trim()
    if (!task) { setTwError(t('chat.errors.teamwork_task_required')); return }
    setTwLaunching(true)
    setTwError('')
    // issue#12 R3 (2026-10-13): 注释承诺「createSession 失败→弹错并保留
    // 旧 session + 旧画面」, 实际 (b) 已先 setMessages([])+删 wsKey,
    // 失败时旧画面/工作目录绑定不可恢复——前端注释与行为脱节。
    // 修法: 把 (b) 块延后到 createSession 成功之后, 失败路径根本不动
    // 旧 messages / wsKey / workspace。保留 (a) 在 try 开头是因为
    // abort 是「取消上一个 turn」的清理动作,与新 session 无关,在 try
    // 内做更安全(避免某个旧 stream 续写)。
    try {
      // Issue#8 (2026-10-13): Team Work 必须把消息锚到全新 sid。launch
      // 服务端已加入「一 run 一 session」守卫, 老 sid 复用直接 409。
      // UI 这边三件事必须发生,顺序不可错:
      //   (a) 取消上一个 turn 的 abortRef + 清状态 (避免旧 stream
      //       在新 session 上续写消息)
      //   (b) [延迟] createSession 成功后才清空 messages + 删 wsKey
      //       + 清 workspace —— 失败路径必须保留旧 session/旧画面
      //       (issue#12 R3 实锤修复: 旧代码这里就清,createSession
      //       失败时旧 session 上下文不可恢复)
      //   (c) createSession 拿新 sid, setSessionId 立即切, 然后 runStream
      //   (d) createSession 失败 → 弹错并保留旧 session + 旧画面
      // 任意一步失败不应破旧 session 状态。

      // (a) 取消旧 stream (如果上轮 chatStream 没正常结束的话)
      try {
        if (abortRef.current) abortRef.current.abort()
      } catch { /* 静默 */ }

      // 1. 建真实 Hermes session(session_id 由 hermes 分配, 消息会落进 gateway)
      const sid = await g.createSession()
      // 1a. createSession 成功后才执行 (b): 立即清空旧画面,把 session 切
      //     回「新会话」占位形态: 旧 messages 留了会让用户产生「新任务
      //     已经接上了旧 context」的错觉 (issue #8 主诉之一)。
      //     [R3] 这段从 try 开头延后到这里:createSession 失败时整段
      //     不执行 → 旧 session + 旧 messages + 旧 wsKey + 旧 workspace
      //     全部完好保留。
      setMessages([])
      setError('')
      setRunInfo(null)
      setPhase(null)
      // issue #75 B3: 新 run 的 session = 无历史, 分页游标/去重集一并重置
      histMetaRef.current = { hasMore: false, beforeCursor: null }
      seenIdsRef.current = new Set()
      setHistMore(false)
      // 同时清 workspace 旧绑定:draft 后再让用户挑, 不沿用旧 sid 的
      // 工作目录,避免旧 run 的产物污染新 run 文件名空间
      try {
        if (sessionId) localStorage.removeItem(wsKeyFor(sessionId))
      } catch { /* ignore */ }
      setWorkspace('')

      // Issue#8: UI 把新 sid 设到 React state + localStorage 与工作目录
      // 迁移的顺序遵循「先写 localStorage 再 setState」避免组件在
      // setMessages([]) 之前有时间窗口把旧 messages 推到新 sid 上;
      // 同时 currentSessionIdRef 也同步 (供 polling 双层保险比对)。
      localStorage.setItem(sidKey, sid)
      setSessionId(sid)
      currentSessionIdRef.current = sid
      // 1b. 工作目录: 弹窗里选的优先, 否则继承 draft/session 绑定
      const ws = twWorkspace || migrateDraftWorkspace(sid)
      if (ws) localStorage.setItem(wsKeyFor(sid), ws)
      // 2. 拼首条消息: 需求 + 参考 workflow + 严格多 agent 细分指令。
      //    这条作为真实用户消息发给 main, main 用 mw-task-dispatch 拆解并下发。
      // I18N-R3 (2026-10-16, REQ §3.4 #8): 字段名按 i18next 切换; marker
      //     `【Team Work 多智能体任务】` 协议字面保持不变 (en/zh 前端都识
      //     别 marker); 正文 `需求:` / `Requirement:` 等走 t(), 前端
      //     parseDispatchCard 双键兼容 (此发送端只发当前语种)。
      const wfHint = (twWorkflow && twWorkflow !== 'auto')
        ? t('chat.team_work_dispatch_field_workflow_explicit', { wf: twWorkflow })
        : t('chat.team_work_dispatch_field_workflow_auto')
      const wsHint = ws
        ? t('chat.team_work_dispatch_field_workspace_set', { ws })
          + t('chat.team_work_dispatch_field_workspace_set_launch', { ws })
        : t('chat.team_work_dispatch_field_workspace_unset')
          + t('chat.team_work_dispatch_field_workspace_unset_suffix')
      // Issue#8 (2026-10-13): 首条消息里明确告诉 main「launch 时
      // session_id = 我们刚创建的 sid」, 杜绝 main 从对话历史复制
      // 上一个 session 的 sid 再调 launch。后端会验证;前端补强语义。
      const sidHint = t('chat.team_work_dispatch_sid_hint_prefix', { sid })
                     + t('chat.team_work_dispatch_sid_hint_body')
      const firstMsg = [
        t('chat.team_work_dispatch_marker'),
        ``,
        t('chat.team_work_dispatch_field_requirement', { task }),
        `${wfHint}`,
        `${wsHint}`,
        ``,
        t('chat.team_work_dispatch_instruction_skill'),
        t('chat.team_work_dispatch_instruction_no_repeat'),
        `${sidHint}`,
      ].join('\n')
      // 3. 显示用户消息 + 流式发给 main(消息落进真实 session)
      setMessages([{ role: 'user', text: firstMsg, ts: Date.now() / 1000 }])
      setListOpen(false)
      setTwOpen(false)
      setTwLaunching(false)
      await runStream(firstMsg, sid)
    } catch (e) {
      setTwError(String(e.message || e))
      setTwLaunching(false)
    }
  }

  const switchSession = async (sid) => {
    if (busy || sid === sessionId) { setListOpen(false); return }
    setListOpen(false)
    setLoadingHist(true)
    setError('')
    // issue #75 s10 C-1: 切换失败也要能重试 —— 失败时记下目标 sid 到
    // histPendingSidRef (Retry 按钮据此重放这次切换), 并把错误呈现在
    // 历史错误条 (histLoadFailed) 上, 而不是只写全局 error 后无路可退。
    //
    // seenIdsRef 快照: 下面切会话要重置去重集; 若加载失败, 画面仍是旧会话
    // 的消息, 而去重集已被清空 → 4s 轮询会把旧消息当新消息重复追加。
    // 故失败时还原快照 (C-1 失败路径的状态一致性, 非新增功能)。
    const seenSnapshot = seenIdsRef.current
    try {
      // issue #75 B3: 切会话 = 重置游标/去重集, 重开一个窗口批次
      // (INV-02: 仍能恢复该 session 历史; 只不再拉全量)。
      histMetaRef.current = { hasMore: false, beforeCursor: null }
      seenIdsRef.current = new Set()
      setHistMore(false)
      await loadHistoryWindow(sid, null)
      setSessionId(sid)
      // 切换成功 → 清掉失败态 (C-1: 错误条与待重试标记都归位)
      setHistLoadFailed(false)
      setHistErr('')
      histPendingSidRef.current = ''
    } catch (e) {
      seenIdsRef.current = seenSnapshot   // 还原去重集 (旧会话消息仍在画面上)
      histPendingSidRef.current = sid     // 记下待重试的目标会话
      setHistLoadFailed(true)
      setHistErr(String(e.message || e))
    } finally {
      setLoadingHist(false)
    }
  }

  return (
    <div className="chat-touch flex flex-col h-full -m-3 md:-m-6">
      {/* 工具条 (CONSOLE2 §4.3: flex-wrap 双行, 375px 不溢出; 裸 session_id 收进 title) */}
      <div className="shrink-0 flex flex-wrap items-center gap-2 px-3 md:px-6 py-2 border-b border-line bg-panel/40 relative">
        <button onClick={openList} disabled={busy || loadingHist}
          className="btn-ghost btn-sm flex items-center gap-1.5 max-w-[320px] flex-1 md:flex-none"
          title={sessionId || t('chat.session.new_session_title')}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
               strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4 shrink-0">
            <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
          </svg>
          <span className="truncate">
            {loadingHist ? t('chat.session.history_loading')
              : sessionId ? (sessions.find(s => s.id === sessionId)?.title || t('chat.session.connected_session'))
              : t('chat.session.new_btn')}
          </span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" className="w-3.5 h-3.5 shrink-0">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
        {/* 工作目录选择器: 当前对话的 workspace, 透传给所有 worker */}
        <div className="relative">
          <button onClick={() => { if (!wsOpen) loadWorkspaces(); setWsOpen(!wsOpen) }}
                  disabled={busy}
                  className="btn-ghost btn-sm flex items-center gap-1.5 max-w-[200px]"
                  title={t('chat.input.workspace_label', { path: workspace || '' })}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
                 strokeLinecap="round" strokeLinejoin="round" className="w-3.5 h-3.5 shrink-0">
              <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z" />
            </svg>
            <span className="truncate font-mono text-[11px]" title={workspace || ''}>
              {workspace ? workspace.split('/').pop() : t('chat.errors.teamwork_unspecified_path')}
            </span>
          </button>
          {wsOpen && (
            <div className="absolute top-full left-0 mt-1 w-[min(340px,calc(100vw-24px))] max-h-[380px] overflow-y-auto
                          rounded-xl bg-panel border border-line shadow-xl z-50 py-1">
              <div className="px-3 py-1.5 text-[11px] text-dim border-b border-line font-mono truncate"
                   title={wsRoot}>
                {t('chat.input.workspace_root_label', { root: wsRoot })}
              </div>
              {workspaces.map(w => (
                <button key={w.path} onClick={() => pickWorkspace(w.path)}
                        className={`w-full text-left px-3 py-2 text-[12.5px] hover:bg-panel2
                                  flex items-center gap-2 ${workspace === w.path ? 'bg-accent/10 text-accent' : ''}`}>
                  <span className="font-mono truncate">{w.name}</span>
                  {!w.writable && <span className="text-[10px] text-bad shrink-0">{t('chat.session.readonly_badge')}</span>}
                  {workspace === w.path && <span className="ml-auto shrink-0">✓</span>}
                </button>
              ))}
              {workspaces.length === 0 && (
                <div className="px-3 py-4 text-center text-[12px] text-dim">{t('chat.input.workspace_empty')}</div>
              )}
              <div className="border-t border-line mt-1 pt-2 px-3 pb-2">
                <div className="text-[11px] text-dim mb-1.5">{t('chat.input.workspace_create_btn')}</div>
                <div className="flex gap-1.5">
                  <input value={wsNewName} onChange={e => setWsNewName(e.target.value)}
                         onKeyDown={e => e.key === 'Enter' && createWorkspace()}
                         placeholder={t('chat.input.name_placeholder')}
                         className={`flex-1 min-w-0 rounded-md bg-canvas border px-2 py-1.5
                                  text-[12px] outline-none focus:border-accent
                                  ${wsCreateError ? 'border-bad' : 'border-line'}`} />
                  <button onClick={createWorkspace} disabled={wsBusy || !wsNewName.trim()}
                          className="btn-primary btn-sm shrink-0">{t('common.create')}</button>
                </div>
                {/* 新建错误就近显示 (§4.7) */}
                {wsCreateError && <div className="field-err">{wsCreateError}</div>}
                {!wsCreateError && <div className="text-[10px] text-dim mt-1.5">{t('chat.multi_agent_card.id_charset_hint')}</div>}
              </div>
            </div>
          )}
        </div>
        {/* Issue #46: 文件浏览器入口 (顶栏工作目录选择器右侧) */}
        <button onClick={() => setFbOpen((v) => !v)}
                disabled={!workspace}
                className={`btn-ghost btn-sm flex items-center gap-1.5 ${fbOpen ? 'bg-accent/10 text-accent' : ''}`}
                title={workspace ? t('chat.files.btn_label') : t('chat.files.no_workspace_hint')}
                aria-label={t('chat.files.btn_label')}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
               strokeLinecap="round" strokeLinejoin="round" className="w-3.5 h-3.5 shrink-0">
            <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z" />
          </svg>
          <span className="hidden sm:inline">{t('chat.files.btn_label')}</span>
        </button>
        <div className="ml-auto flex items-center gap-2">
          {/* 2026-10-15 改版:多智能体协作入口已迁至全局 header「核心功能」按钮
              (App.jsx 全局动作组 + 移动顶栏),本对话页右上角不再重复出现。
              空态引导保留(ChatView 内的「发起多智能体协作」按钮,R2.5)。*/}
          <button onClick={newChat} className="btn-ghost btn-sm" disabled={busy}>{t('chat.session.new_btn')}</button>
          {onClose && (
            <button onClick={onClose} className="btn-ghost btn-sm !text-bad"
                    title={t('app.close_chat')}>
              {t('app.close_chat')}
            </button>
          )}
        </div>

        {/* 会话列表下拉 (裸 session_id 收进列表项 title, §4.3/AC-16) */}
        {listOpen && (
          <div className="absolute top-full left-3 right-3 md:left-4 md:right-auto md:w-[420px] max-h-[60vh] md:max-h-[380px] overflow-y-auto
                        rounded-xl bg-panel border border-line shadow-xl z-40 py-1">
            {sessions.length === 0 && (
              <div className="px-4 py-6 text-center text-[12.5px] text-dim">{t('chat.session.history_empty')}</div>
            )}
            {sessions.map((s) => {
              const active = s.id === sessionId
              return (
                <button key={s.id} onClick={() => switchSession(s.id)} title={s.id}
                  className={`w-full text-left px-4 py-2.5 border-b border-line/50 last:border-0
                             transition-colors ${active ? 'bg-accent/10' : 'hover:bg-panel2'}`}>
                  <div className="flex items-center gap-2">
                    <span className={`text-[13px] font-medium truncate flex-1
                                    ${active ? 'text-accent' : 'text-ink'}`}>
                      {s.title || s.preview || t('chat.errors.title_unspecified')}
                    </span>
                    <span className="text-[10.5px] text-dim shrink-0">{fmtTime(s.last_active)}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-0.5 text-[11px] text-dim">
                    <span className="shrink-0">{s.message_count} {t('chat.session.message_count_suffix')}</span>
                    {active && <span className="ml-auto text-accent shrink-0">{t('chat.session.current_badge')}</span>}
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </div>

      {/* 消息流 */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 md:px-6 py-4 space-y-3">
        {/* issue #75 B3 §5.3: 「更多历史对话」前插入口 (has_more=true 才显示;
            加载态 disabled+.spin; 错误态内联 .field-err, 按钮恢复可重试) */}
        {histMore && (
          <div className="flex flex-col items-center gap-1 py-1">
            <button onClick={loadMoreHistory} disabled={loadingMore}
                    className="btn-ghost btn-sm flex items-center gap-1.5">
              {loadingMore && <span className="spin w-3.5 h-3.5" />}
              {loadingMore ? t('common.loading') : t('chat.session.load_more')}
            </button>
            {moreErr && (
              <div className="field-err text-[11.5px]">
                {t('chat.errors.more_history_failed', { err: moreErr })}
              </div>
            )}
          </div>
        )}
        {/* 首屏历史未就绪: 骨架气泡组, 防 CLS (§4.5) */}
        {messages.length === 0 && !histLoaded && (
          <div className="space-y-3" aria-busy="true" aria-label={t('chat.session.load_history_aria')}>
            <div className="flex justify-end"><div className="skeleton w-[40%] h-14 rounded-2xl" /></div>
            <div className="flex justify-start"><div className="skeleton w-[65%] h-[88px] rounded-2xl" /></div>
            <div className="flex justify-end"><div className="skeleton w-[35%] h-10 rounded-2xl" /></div>
            <div className="flex justify-start"><div className="skeleton w-[55%] h-[72px] rounded-2xl" /></div>
          </div>
        )}
        {/* issue #75 s10 C-1: 首拉/切换失败错误条 + 显式「重试」按钮。
            (D-1 修完白屏后, 失败态是用户唯一能看到的错误面 —— 无重试入口
            等于「失败即死路」, 必须闭环。错误条与空态互斥: 有失败态时不
            渲染空态引导 (避免「没消息 + 出错」双重语义混淆, AC-15)。 */}
        {histLoadFailed && (
          <div className="flex items-center justify-between gap-3 px-4 py-2.5 rounded-xl
                          bg-bad/10 border border-bad/30 text-[12.5px] text-bad"
               role="alert">
            <span className="min-w-0 break-words">
              {t('chat.errors.load_session_history_failed', { err: histErr })}
            </span>
            <button onClick={retryHistoryLoad} disabled={histLoading}
                    className="btn-ghost btn-sm shrink-0 flex items-center gap-1.5">
              {histLoading && <span className="spin w-3.5 h-3.5" />}
              {histLoading ? t('common.loading') : t('common.retry')}
            </button>
          </div>
        )}
        {/* 空态引导 (§4.6): main 会话带协作入口, worker 对话不带
            (issue #75 s10 C-1: 失败态优先, 不与错误条同时出现) */}
        {histLoaded && messages.length === 0 && !histLoadFailed && (
          <div className="h-full flex flex-col items-center justify-center text-dim gap-2">
            <div className="text-[32px]" aria-hidden="true">💬</div>
            {base ? (
              <>
                <div className="text-[15px] font-semibold text-ink">{t('chat.session.chat_with', { name: targetLabel })}</div>
                <div className="text-[12.5px] text-center">{t('chat.session.empty_hint')}</div>
              </>
            ) : (
              <>
                <div className="text-[15px] font-semibold text-ink">{t('chat.session.chat_with_main')}</div>
                <div className="text-[12.5px] text-center">
                  {t('chat.session.empty_hint_intro')}
                </div>
                <button className="btn-primary mt-2" onClick={openTeamWork}>{t('chat.session.start_teamwork_btn')}</button>
              </>
            )}
          </div>
        )}
        {messages.map((m, i) => {
          const mTs = m.ts && fmtTime(m.ts)
          // 协作任务摘要卡: 只影响渲染呈现, 发送内容零改动 (§4.2)
          const card = m.role === 'user' ? parseDispatchCard(m.text) : null
          return (
          <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            {card ? (
              <div className="max-w-[88%] md:max-w-[78%] space-y-1">
                <div className="card rounded-2xl px-3.5 py-2.5 border-l-[3px] border-l-accent text-[12.5px] space-y-1">
                  <div className="font-bold text-ink flex items-center gap-1.5">{t('chat.multi_agent_card.title')}</div>
                  <div className="text-ink leading-relaxed break-words" title={card.req}>
                    {t('chat.multi_agent_card.requirement')} {card.req}
                  </div>
                  <div className="text-dim" title={card.wf}>{t('chat.multi_agent_card.flow')} {card.wf === '__AUTO_SELECT__' ? t('chat.dispatch_card.auto_select') : card.wf}</div>
                  {card.ws && <div className="text-dim" title={card.ws}>{t('chat.multi_agent_card.workspace')} {card.ws}</div>}
                  <details className="tech-details mt-1">
                    <summary>{t('chat.multi_agent_card.view_dispatch')}</summary>
                    <pre className="code-block mt-1.5 text-[11px] max-h-[240px]">{m.text}</pre>
                  </details>
                </div>
                {mTs && (
                  <div className="text-[10.5px] text-dim text-right pr-1 select-none"
                       title={m.ts ? new Date(m.ts * 1000).toLocaleString() : ''}>
                    {mTs}
                  </div>
                )}
              </div>
            ) : m.role === 'user' ? (
              <div className="max-w-[88%] md:max-w-[78%] space-y-1">
                <div className="rounded-2xl px-3 md:px-4 py-2 md:py-2.5 text-[13px] md:text-[13.5px] leading-relaxed
                             bg-accent/20 text-ink rounded-br-md whitespace-pre-wrap">
                  {m.text}
                </div>
                {mTs && (
                  <div className="text-[10.5px] text-dim/70 text-right pr-1 select-none"
                       title={m.ts ? new Date(m.ts * 1000).toLocaleString() : ''}>
                    {mTs}
                  </div>
                )}
              </div>
            ) : m.role === 'toolgroup' ? (() => {
              // 工具组合并折叠卡 (DESIGN C-5, AC1-3): 折叠态 = N 次工具调用 +
              // 工具名去重摘要; 状态点: 任一 running→黄脉冲 / 有 error→红 / 全 done→绿。
              // 默认折叠, 运行中不自动展开 (B-AC2); 用户点开展开后 <details> 自治。
              const items = m.items || []
              const running = items.some(it => it.status === 'running')
              const hasErr = items.some(it => it.status === 'error')
              // 工具名去重计数摘要: read_file ×2, terminal ×3
              const cnt = {}
              items.forEach(it => { cnt[it.name] = (cnt[it.name] || 0) + 1 })
              const summary = Object.entries(cnt).map(([n, c]) => c > 1 ? `${n} ×${c}` : n).join(', ')
              return (
                <div className="max-w-[92%] md:max-w-[82%] w-full">
                  <details className="rounded-xl bg-panel/50 border border-line/70 px-3 py-1.5"
                          >
                    <summary className="cursor-pointer text-[11.5px] select-none
                                       flex items-center gap-1.5 hover:text-ink transition-colors">
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                        running ? 'bg-warn animate-pulse'
                        : hasErr ? 'bg-bad' : 'bg-ok/70'}`} />
                      <span className="font-medium text-ink/80">{t('chat.tool_call.calls_count', { n: items.length })}</span>
                      <span className="text-dim/60 truncate min-w-0 font-mono" title={summary}>· {summary}</span>
                    </summary>
                    {/* 展开后逐条目渲染, 复用单卡内部结构, 状态随 items 实时更新 */}
                    <div className="mt-1.5 space-y-1.5">
                      {items.map((it, j) => (
                        <div key={j} className="rounded-lg bg-canvas/40 border border-line/50 px-2.5 py-1.5">
                          <div className="flex items-center gap-1.5 text-[11.5px]">
                            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                              it.status === 'running' ? 'bg-warn animate-pulse'
                              : it.status === 'error' ? 'bg-bad' : 'bg-ok/70'}`} />
                            <span className="font-mono font-medium text-ink/80">{it.name}</span>
                            <span className="text-dim/60 shrink-0" title={it.status}>
                              {it.status === 'running' ? t('chat.tool_call.running_busy')
                              : it.status === 'error' ? t('chat.tool_call.failed') : t('chat.tool_call.done')}</span>
                            {it.args && <span className="text-dim/50 truncate min-w-0">· {it.args.split('\n')[0].slice(0, 60)}</span>}
                          </div>
                          {it.args && (
                            <div className="mt-1">
                              <div className="text-[10.5px] text-dim/70 mb-0.5">{t('chat.tool_call.args')}</div>
                              <pre className="text-[11px] text-dim leading-snug whitespace-pre-wrap break-all
                                             bg-canvas/60 border border-line/50 rounded-lg p-2 max-h-[200px] overflow-y-auto font-mono">{it.args}</pre>
                            </div>
                          )}
                          {it.result != null && (
                            <div className="mt-1">
                              <div className="text-[10.5px] text-dim/70 mb-0.5">{t('chat.tool_call.result')}</div>
                              <pre className={`text-[11px] leading-snug whitespace-pre-wrap break-all
                                             border rounded-lg p-2 max-h-[240px] overflow-y-auto font-mono ${
                                it.status === 'error' ? 'text-bad bg-bad/5 border-bad/30'
                                : 'text-dim bg-canvas/60 border-line/50'}`}>{it.result}</pre>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                </div>
              )
            })() : m.role === 'turnthinking' ? (
              // 回合级真思考卡 (DESIGN §2.5, issue #1): 纯文本渲染, 绝不过
              // md()/dangerouslySetInnerHTML (R5 XSS 红线); 默认折叠不自动展开。
              <div className="max-w-[88%] md:max-w-[78%] w-full">
                <details className="rounded-xl bg-panel/60 border border-line/70 px-3 py-2">
                  <summary className="cursor-pointer text-[11.5px] text-dim select-none
                                     flex items-center gap-1.5 hover:text-ink transition-colors">
                    <span className={`w-1.5 h-1.5 rounded-full ${m.streaming ? 'bg-warn animate-pulse' : 'bg-dim/60'}`} />
                    <span className="font-medium">{m.streaming ? t('chat.tool_call.thinking_busy') : t('chat.tool_call.thinking')}</span>
                    <span className="text-dim/60">({(m.text || '').length} {t('chat.tool_call.chars_suffix')})</span>
                  </summary>
                  <div className="mt-2 text-[11.5px] text-dim leading-relaxed whitespace-pre-wrap
                                 border-l-2 border-warn/40 pl-2.5 max-h-[240px] overflow-y-auto">
                    {m.text}
                  </div>
                </details>
              </div>
            ) : (
              <div className="max-w-[88%] md:max-w-[78%] w-full space-y-2">

                {m.commentary && (
                  <div className="text-[12px] text-dim italic border-l-2 border-line pl-2.5 whitespace-pre-wrap">
                    {m.commentary}
                  </div>
                )}
                {(m.text || m.streaming) && (
                  <div className="rounded-2xl px-3 md:px-4 py-2 md:py-2.5 text-[13px] md:text-[13.5px] leading-relaxed
                                 bg-panel2 border border-line rounded-bl-md">
                    <div className="md-body" dangerouslySetInnerHTML={md(m.text)} />
                    {m.streaming && !m.text && (
                      <span className="inline-block w-2 h-4 bg-accent/70 animate-pulse rounded-sm" />
                    )}
                  </div>
                )}
                {/* assistant 回合时间标签: 与 user 一样只在 ts 存在时显示 */}
                {mTs && !m.streaming && (
                  <div className="text-[10.5px] text-dim/70 pl-1 select-none"
                       title={m.ts ? new Date(m.ts * 1000).toLocaleString() : ''}>
                    {mTs}
                  </div>
                )}
              </div>
            )}
          </div>
          )
        })}
        {/* 处理中状态条: 阶段 + 计时, 让用户随时确认 agent 在干活 */}
        {busy && phase && (
          <div className="sas-status-bar">
            <span className="spin" />
            {phase === 'queued' && (<>{t('chat.tool_call.sending_request')}</>)}
            {phase === 'thinking' && (
              <span className="flex items-center gap-1.5">
                {t('chat.tool_call.thinking_busy')}
                <span className="flex gap-1 items-center h-3 px-0.5">
                  <span className="sas-typing-dot" /><span className="sas-typing-dot" /><span className="sas-typing-dot" />
                </span>
              </span>
            )}
            {phase === 'tool' && (
              <span className="flex items-center gap-1.5">
                {t('chat.tool_call.running_tool', { name: runInfo?.toolName || '…' })}
                <span className="flex gap-1 items-center h-3 px-0.5">
                  <span className="sas-typing-dot" /><span className="sas-typing-dot" /><span className="sas-typing-dot" />
                </span>
              </span>
            )}
            {phase === 'responding' && (<>{t('chat.tool_call.replying')}</>)}
            {phase === 'steering' && (<>{t('chat.input.steer_hint')}</>)}
            <span className="elapsed">{elapsed}s</span>
          </div>
        )}
        {error && (
          <div className="px-4 py-2.5 rounded-xl bg-bad/10 border border-bad/30 text-[12.5px] text-bad">
            {error}
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div className="shrink-0 border-t border-line bg-panel/60 px-3 md:px-6 py-2.5 relative">
        {/* / 命令补全弹层 */}
        {input.startsWith('/') && !input.includes(' ') && (() => {
          const matches = SLASH_COMMANDS.filter(c => c.cmd.startsWith(input.toLowerCase()))
          if (matches.length === 0) return null
          const idx = Math.min(slashIdx, matches.length - 1)
          return (
            <div className="absolute bottom-full left-3 right-3 md:left-6 md:right-auto md:w-[360px] rounded-xl bg-panel
                          border border-line shadow-xl z-40 py-1">
              {matches.map((c, i) => (
                <button key={c.cmd}
                  onMouseDown={(e) => { e.preventDefault(); setInput(c.cmd + ' '); setSlashIdx(0) }}
                  className={`w-full text-left px-4 py-2 flex items-baseline gap-3
                             ${i === idx ? 'bg-accent/15 text-accent' : 'hover:bg-panel2'}`}>
                  <span className="font-mono text-[13px] font-semibold">{c.cmd}</span>
                  <span className="text-[11.5px] text-dim">{c.desc}</span>
                </button>
              ))}
              <div className="px-4 pt-1.5 text-[10.5px] text-dim/60 border-t border-line/50">
                {t('chat.input.completion_hint')}
              </div>
            </div>
          )
        })()}
        {/* 待发送附件 */}
        {pendingFiles.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {pendingFiles.map((f, i) => (
              <div key={i} className="flex items-center gap-2 rounded-lg bg-panel2 border border-line px-2.5 py-1.5 text-[11.5px]">
                <span className="text-ink truncate max-w-[200px]">{f.name}</span>
                <span className="text-dim">{(f.bytes / 1024).toFixed(1)} KB</span>
                <button onClick={() => setPendingFiles((p) => p.filter((_, j) => j !== i))}
                        className="text-dim hover:text-bad">✕</button>
              </div>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          <input ref={fileRef} type="file" multiple className="hidden"
                 onChange={(e) => uploadFiles(e.target.files)} />
          <button onClick={() => fileRef.current?.click()} disabled={busy || uploading}
                  className="btn-ghost btn-sm shrink-0 self-stretch flex items-center"
                  title={t('chat.input.attach')}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
                 strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <textarea
            value={input}
            onChange={(e) => { setInput(e.target.value); setSlashIdx(0) }}
            onKeyDown={(e) => {
              const matches = input.startsWith('/') && !input.includes(' ')
                ? SLASH_COMMANDS.filter(c => c.cmd.startsWith(input.toLowerCase()))
                : []
              if (matches.length > 0 && e.key === 'ArrowDown') {
                e.preventDefault()
                setSlashIdx((i) => (Math.min(i, matches.length - 1) + 1) % matches.length)
                return
              }
              if (matches.length > 0 && e.key === 'ArrowUp') {
                e.preventDefault()
                setSlashIdx((i) => (Math.max(i, 0) - 1 + matches.length) % matches.length)
                return
              }
              if (matches.length > 0 && e.key === 'Tab') {
                e.preventDefault()
                setInput(matches[Math.min(slashIdx, matches.length - 1)].cmd + ' ')
                setSlashIdx(0)
                return
              }
              if (e.key === 'Escape') { setSlashIdx(0); if (input.startsWith('/')) { setInput('') } return }
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
            }}
            placeholder={t('chat.input.placeholder')}
            rows={2}
            className="flex-1 resize-none rounded-xl bg-panel2 border border-line px-3.5 py-2.5
                       text-[13.5px] text-ink placeholder:text-dim/60 outline-none
                       focus:ring-1 focus:ring-accent/50"
          />
          {busy && (
            <button onClick={stop} className="btn-ghost btn-sm !text-bad shrink-0">{t('chat.input.stop')}</button>
          )}
          <button onClick={send} disabled={(!input.trim() && pendingFiles.length === 0) || booting}
            className="btn-primary btn-sm shrink-0"
            title={busy ? t('chat.input.steer_hint') : t('chat.input.send')}>
            {busy ? t('chat.input.steer_to_current_short') : t('chat.input.send')}
          </button>
        </div>
      </div>

      {/* ── 多智能体协作启动弹窗 (CONSOLE2 §4.4: 迁移 ui.jsx Modal,
           获得 Escape/遮罩关闭/弹入动画; 提交中防误关走 closeTeamWork 既有守卫) ── */}
      <Modal open={twOpen} onClose={closeTeamWork} title={t('chat.team_work_modal.title')} width="max-w-[560px]"
        footer={
          <>
            <button onClick={closeTeamWork} disabled={twLaunching}
                    className="btn-ghost btn-sm">{t('common.cancel')}</button>
            <button onClick={submitTeamWork} disabled={twLaunching || !twTask.trim()}
                    className="btn-primary btn-sm">
              {twLaunching ? (<><span className="spin" />{t('chat.team_work_modal.start_busy_btn')}</>) : t('chat.team_work_modal.start_btn')}
            </button>
          </>
        }>
        {twOpen && (
          <div className="px-5 py-4 space-y-4">
            <div className="text-[11.5px] text-dim -mt-1">
              {t('chat.team_work_modal.subtitle')}
            </div>
            <div>
              <label className="label">{t('chat.team_work_modal.task_requirement')} <span className="text-bad">*</span></label>
              <textarea
                value={twTask}
                onChange={(e) => setTwTask(e.target.value)}
                disabled={twLaunching}
                rows={5}
                autoFocus
                placeholder={t('chat.team_work_modal.task_placeholder')}
                className="w-full resize-none rounded-lg bg-canvas border border-line px-3 py-2.5
                           text-[13px] text-ink placeholder:text-dim/60 outline-none
                           focus:border-accent focus:ring-2 focus:ring-accent/25"
              />
              <div className="hint">{t('chat.team_work_modal.task_requirement_hint')}</div>
            </div>

            <div>
              <label className="label">Workflow <span className="text-dim font-normal">{t('chat.team_work_modal.workspace_placeholder')}</span></label>
              <select value={twWorkflow} onChange={(e) => setTwWorkflow(e.target.value)}
                      disabled={twLaunching}
                      className="input cursor-pointer">
                <option value="auto">{t('chat.team_work_modal.auto_select')}</option>
                {twWorkflows && Object.entries(twWorkflows).map(([id, w]) => {
                  if (id === 'auto') return null
                  return (
                    <option key={id} value={id} title={w.description || ''}>
                      {w.name || id}{w.description ? ` — ${w.description}` : ''}
                    </option>
                  )
                })}
              </select>
              <div className="hint">
                {twWorkflows
                  ? t('chat.team_work_modal.workflows_count', { n: Object.keys(twWorkflows).filter(k => k !== 'auto').length })
                  : t('chat.team_work_modal.workflows_unreachable')}
              </div>
            </div>

            <div>
              <label className="label">{t('chat.team_work_modal.workspace_label')} <span className="text-dim font-normal">{t('chat.team_work_modal.workspace_placeholder')}</span></label>
              <select value={twWorkspace} onChange={(e) => setTwWorkspace(e.target.value)}
                      disabled={twLaunching}
                      className="input cursor-pointer">
                <option value="">
                  {t('chat.team_work_modal.inherit_option', { name: workspace ? workspace.split('/').pop() : t('chat.errors.teamwork_unspecified_path') })}
                </option>
                {workspaces.map(w => (
                  <option key={w.path} value={w.path} title={w.path}>{w.name}</option>
                ))}
              </select>
              <div className="hint">
                {t('chat.team_work_modal.workspace_hint')}
              </div>
            </div>

            {twError && (
              <div className="px-3 py-2 rounded-lg bg-bad/10 border border-bad/30 text-[12.5px] text-bad" title={twError}>
                {twError}
              </div>
            )}
          </div>
        )}
      </Modal>
      {/* Issue #46 (s4-impl): 文件浏览器抽屉。key={workspace} 整体重挂载,
          切工作目录/切会话时树与预览天然重置(AC-A5 / R4.5)。
          project 必须只传项目名(basename), 绝对路径会被后端正则拒绝 (R-2 / ARCH §4.1)。 */}
      <FileDrawer key={workspace || ''}
                  open={fbOpen && !!workspace}
                  project={workspace ? workspace.split('/').filter(Boolean).pop() || '' : ''}
                  revealTarget={fbReveal}
                  onClose={() => { setFbOpen(false); setFbReveal(null) }} />
    </div>
  )
})

// [en] Default export kept under the same name so App.jsx import (`import
//      ChatView from './views/ChatView.jsx'`) and ref usage both work.
// [zh-cn] default export 保持同名,App.jsx 的 `import ChatView from ...` 和
//        ref 用法都直接可用。
export default ChatView
