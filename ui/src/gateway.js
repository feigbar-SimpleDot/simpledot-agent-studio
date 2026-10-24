/* Main-agent / per-worker gateway over SSE.
   Builds an EventSource and emits a normalised stream of events
   (connect / message / tool_call / turn_end / closed / error).
   Used by ChatView for both the main session and per-worker sessions. */
// gateway.js — 对话页与 main agent gateway 的连接层。
//
// 部署形态: 浏览器 → console(:8700) 同源代理 → gateway(:8642)。
// 代理在 mw/console.py (_proxy_to_gateway), 密钥由服务端环境变量注入,
// 永不下发到浏览器; 同源也彻底绕开 gateway SSE 缺 CORS 头的问题
// (api_server.py StreamResponse.prepare 早于 cors_middleware 加头)。
//
// 端点(经代理透传, gateway/platforms/api_server.py):
//   POST /api/sessions                    新建会话
//   GET  /api/sessions                    会话列表
//   GET  /api/sessions/{id}/messages      历史消息
//   POST /api/sessions/{id}/chat          同步对话
//   POST /api/sessions/{id}/chat/stream   SSE 流式对话

// 同源相对路径: base 为空, 密钥不进浏览器。
export function gwBase() { return '' }
export function gwKey() { return '' }
export function gwSetConfig() { /* 代理模式下无需前端配置 */ }
export function gwConfigured() { return true }

// 探测后端代理是否就绪(旧版 console 无此端点时前端给出提示)。
export async function gwBootstrap() {
  try {
    const r = await fetch('/api/gwconfig')
    if (!r.ok) return null
    return await r.json()
  } catch {
    return null
  }
}

async function j(r) {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try {
      const d = await r.json()
      msg = d.error?.message || d.error || d.message || msg
      if (typeof msg !== 'string') msg = JSON.stringify(msg)
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return r.json()
}

// 工厂: 生成指向指定代理前缀的 gateway 客户端。
// base='' → main agent gateway(经 /api/sessions* 同源代理);
// base='/api/w/<worker>' → 指定 worker 的 gateway(经 /api/w/ 同源代理,
// 密钥同样由 console 从注册表注入, 不进浏览器)。
export function makeGw(base) {
  const u = (path) => `${base}${path}`
  return {
  // 会话列表 (Phase 1 用)
  async listSessions(limit = 50) {
    return j(await fetch(u(`/api/sessions?limit=${limit}`)))
  },

  async createSession() {
    const d = await j(await fetch(u('/api/sessions'),
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }))
    return d.session?.id || d.id
  },

  // issue #75 B3 §5.2: opts={want, before} 走代理层窗口化端点 (只发 want/
  // before, 代理据此过滤+分页); 不传 opts = 旧全量行为 (§9 向后兼容,
  // reasoning 轮询 fetchMessages 走这条 —— 回合下界/游标语义依赖全量数组,
  // 窗口化会破坏 turnLowerBound, 见 IMPL-NOTES 偏差登记)。
  async getMessages(sessionId, opts = null) {
    const q = opts
      ? (() => {
          const sp = new URLSearchParams()
          if (opts.want) sp.set('want', String(opts.want))
          if (opts.before) sp.set('before', String(opts.before))
          return `?${sp.toString()}`
        })()
      : ''
    return j(await fetch(u(`/api/sessions/${encodeURIComponent(sessionId)}/messages${q}`)))
  },

  // 向运行中的 run 注入转向指令(Hermes 原生 POST /v1/runs/{run_id}/steer)。
  // run 非 running 状态返回 409。
  async steer(runId, text) {
    return j(await fetch(u(`/v1/runs/${encodeURIComponent(runId)}/steer`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: text }),
    }))
  },

  // 停止运行中的 run(Hermes 原生 POST /v1/runs/{run_id}/stop)。
  // issue #63 配套: 代理保活(下游断→排空)落地后, abort 本地 fetch 不再等于
  // 停止回合 —— 用户显式点「停止」必须先经本端点让 gateway 真中断, 再 abort
  // 本地流。组件卸载/切会话/关页面只 abort 不调本方法(那正是要保活的场景)。
  async stop(runId) {
    return j(await fetch(u(`/v1/runs/${encodeURIComponent(runId)}/stop`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }))
  },

  // SSE 流式对话。content: 纯文本 string 或 OpenAI 多模态 parts 数组
  // (Hermes 原生: _normalize_multimodal_content 支持 text + image_url/data:image)。
  // onEvent(name, payload) 逐事件回调。
  // 事件序列: run.queued → run.started → message.started
  //   → assistant.delta(多次) / assistant.commentary / tool.* → assistant.completed
  //   → run.completed|failed|cancelled
  //
  // 修复: 旧版用单一 '\n\n' 切分且 done 后不 flush, 会在 chunk 边界切到帧中间时
  // 把后续所有帧堆在 buf 里不触发 onEvent, 表现为"chat 流只 push 一两条就静默"。
  // 改为: 同时识别 LF LF 与 CRLF CRLF 两种帧分隔, 解析时归一化 \r,
  //        done 后 flush 残留 buf(若有完整帧则补发), 用 maxFrameBytes 防止病态累积。
  //
  // ★ 进一步修复: reader.read() 失败 / connection reset / network error 时,
  //   gateway 后端常因长 run 中途超时打断 SSE 连接, 导致整个 run 后续消息全部丢失。
  //   我们做三件事:
  //   1) 区分 done=true(server 主动 EOF,run 完成) vs 异常断连 — 后者尝试恢复
  //   2) 异常时 fallback 到快速轮询 GET /api/sessions/{id}/messages(1秒间隔),
  //      抓取 chatStream 中断期间 main agent 已经落地但前端没收到的消息
  //   3) 轮询期间检测 run 是否重新变为 idle, 恢复时也用 onEvent 通知前端
  //      (用 'poll.resumed' 事件标记恢复边界, 避免 ChatView 重复渲染)
  async chatStream(sessionId, content, onEvent, signal) {
    // 包装一层: 内部 fetch 抛错不算 done, 触发 fallback 恢复
    let transportBroken = false
    let streamDone = false
    try {
      const r = await fetch(
        u(`/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: content }),
          signal,
        })
      if (!r.ok) { await j(r) }  // 抛错 — HTTP 4xx/5xx 不恢复, 直接抛出
      if (!r.body) throw new Error('SSE: no response body')

      const reader = r.body.getReader()
      const dec = new TextDecoder('utf-8')
      let buf = ''
      const MAX_BUF = 1 << 20
      const dispatch = (frame) => {
        let ev = 'message', data = null
        for (const raw of frame.split('\n')) {
          const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw
          if (!line) continue
          if (line.startsWith(':')) continue
          if (line.startsWith('event:')) ev = line.slice(6).trim()
          else if (line.startsWith('data:')) {
            const payload = line.slice(5).trim()
            if (payload === '') continue
            try { data = JSON.parse(payload) } catch { /* ignore */ }
          }
        }
        if (data !== null) onEvent(ev, data)
      }
      const flushPending = () => {
        let idx
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          dispatch(frame)
        }
        let idxCR
        while ((idxCR = buf.indexOf('\r\n\r\n')) >= 0) {
          const frame = buf.slice(0, idxCR)
          buf = buf.slice(idxCR + 4)
          dispatch(frame)
        }
      }
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          flushPending()
          streamDone = true
          break
        }
        buf += dec.decode(value, { stream: true })
        if (buf.length > MAX_BUF) buf = buf.slice(-MAX_BUF)
        flushPending()
      }
    } catch (err) {
      // 用户主动 abort 不恢复(AbortError)
      if (err?.name === 'AbortError') throw err
      // 其他网络/解析错误: 标记 broken, 进入 fallback 恢复
      transportBroken = true
      onEvent('poll.broken', { error: String(err?.message || err), session_id: sessionId })
    }
    // ★ Fallback 恢复: SSE 异常断连时, 启动快速轮询抓取落地消息
    // 触发条件: transportBroken && !signal?.aborted
    if (transportBroken && !signal?.aborted) {
      let lastCount = 0
      let baselineSet = false
      let done = false  // fastPoll 检测到 run 结束(fr=stop)后置 true
      const fastPoll = async () => {
        if (signal?.aborted || done) return
        try {
          const d = await fetch(u(`/api/sessions/${encodeURIComponent(sessionId)}/messages`),
            { signal }).then(r => r.ok ? r.json() : null)
          if (!d) return
          const raw = d.data || []
          const count = raw.length
          const last = raw[raw.length - 1]
          if (last && last.role === 'assistant' && last.finish_reason &&
              last.finish_reason !== 'tool_calls') {
            if (count > lastCount) {
              const newRaw = raw.slice(lastCount)
              for (const m of newRaw) {
                onEvent('poll.recovered', { raw: m })
              }
            }
            done = true
            return
          }
          if (!baselineSet) {
            lastCount = count; baselineSet = true
            // 空 messages + 无 fr 立刻当作 run 已结束(无新消息可等)
            if (count === 0) { done = true; return }
            return
          }
          if (count > lastCount) {
            const newRaw = raw.slice(lastCount)
            for (const m of newRaw) {
              onEvent('poll.recovered', { raw: m })
            }
            lastCount = count
          }
        } catch { /* 单轮失败, 下轮继续 */ }
      }
      // 1s 间隔, 最多 5 分钟(防止无限循环)
      const startTime = Date.now()
      while (!signal?.aborted && !done && (Date.now() - startTime) < 5 * 60 * 1000) {
        await fastPoll()
        if (done) break
        await new Promise(r => setTimeout(r, 1000))
      }
    }
    // SSE 正常完成 (done=true 且 streamDone) — 不需要 fallback
      },
  }
}

// main agent 默认客户端(向后兼容既有 import { gw })
export const gw = makeGw('')
