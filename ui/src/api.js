/* Thin REST wrapper around /api/*.
   Each method returns a promise; error messages live with the call sites
   (so views can wrap them with t()) — gateway.js / components are similar. */
// 控制台 API 层 — 与 mw/console.py 一一对应。
// 所有函数返回解析后的 JSON; 出错抛 Error(message)。
import { i18next } from './i18n'

// issue #75 §3 (FR-05 三态归因, 唯一实现口径): 统一 API 错误类型。
// kind ∈ {network, server, client}: fetch reject / status>=500 / 4xx。
// 调用点 catch 后可据 kind 选 t('err.network') / t('err.server') 文案,
// 禁再裸 catch {} 静默 (NFR-4: tester 可用 DOM 文本客观断言)。
export class ApiError extends Error {
  // issue #86: cause 透传原始错误 —— 断网时 fetch reject 的原生 TypeError
  // (含 stack/errno) 不再被转包吃掉, 排障可回到现场。cause 属 Error 标准
  // options (ES2022), 非枚举 ⇒ 不进 JSON.stringify / 展开表面, UI 文案不变。
  constructor(message, { kind = 'server', status = 0, endpoint = '', cause } = {}) {
    super(message, { cause })
    this.name = 'ApiError'
    this.kind = kind      // network | server | client
    this.status = status
    this.endpoint = endpoint
  }
}

const j = async (r, endpoint = '') => {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try { msg = (await r.json()).error || msg } catch { /* ignore */ }
    throw new ApiError(msg, {
      kind: r.status >= 500 ? 'server' : 'client',
      status: r.status,
      endpoint: endpoint || r.url,
    })
  }
  return r.json()
}

// fetch reject (断网/DNS) → network 类 ApiError; 其余原样上抛。
// issue #86: 转包时用 cause 保留原始 error 引用 (原生 TypeError 的 stack 等),
// kind/message/endpoint 语义逐字不变 ⇒ UI 三态归因与文案不受影响。
const net = (p, endpoint) =>
  p.catch((e) => {
    if (e instanceof ApiError) throw e
    throw new ApiError(String(e?.message || e), { kind: 'network', endpoint, cause: e })
  })

// 归因文案: ApiError → t() 双语短句 (§3 模板口径)。非 ApiError 兜底原样。
// t 省略时用 i18next 当前实例 (避免 useCallback 捕获过期 t, 语言切换失效)。
// issue #81-2: 4xx (kind='client') 此前与 5xx 共用 err.server 文案, 把
// 客户端错误说成"服务端错误" —— 会误导排障方向 (改服务端而不是查请求
// 参数/路由/鉴权)。现在 4xx 走 err.client (双语同 commit 新增)。
export const fmtApiError = (e, t) => {
  if (e instanceof ApiError) {
    const tr = t || ((k, o) => i18next.t(k, o))
    const ep = e.endpoint || ''
    if (e.kind === 'network') return tr('err.network', { ep })
    if (e.kind === 'client') return tr('err.client', { ep, code: e.status })
    return tr('err.server', { ep, code: e.status })
  }
  return String(e?.message || e)
}

export const api = {
  // issue #75 B1/B2: 顶栏心跳 (数百字节) 与单任务详情 (列表投影补全通道)
  // issue #75 s9 M-1 (AC-06 P0): 所有 GET 端点统一过 net(fetch(...), ep)
  // —— 断网时 fetch reject 为原生 TypeError, 非 ApiError ⇒ fmtApiError
  // 落兜底 String(e.message) = "Failed to fetch", 无归因文案、无端点路径。
  // 端点标签用【路径模板】形式 (如 /api/workers/:name) 而非插值后的实际
  // 值: 标签必须稳定 (不随调用参数漂移), 且不把用户可控字符串带进错误文案。
  heartbeat: () => net(
    fetch('/api/heartbeat').then((r) => j(r, '/api/heartbeat')),
    '/api/heartbeat'),
  taskDetail: (id) => net(
    fetch(`/api/tasks/${encodeURIComponent(id)}`)
      .then((r) => j(r, '/api/tasks/:id')),
    '/api/tasks/:id'),
  tasks: () => net(fetch('/api/tasks').then((r) => j(r, '/api/tasks')), '/api/tasks'),
  // issue #75 s6 缺陷 2: 轻量轮询端点 —— 只回顶层 team_work_active 段
  // (TasksBoardView 的 5s 轮询只消费这一段; 走完整 /api/tasks 每次拖走
  // 122KB)。后端: GET /api/tasks?fields=active。
  tasksActive: () => net(
    fetch('/api/tasks?fields=active').then((r) => j(r, '/api/tasks?fields=active')),
    '/api/tasks?fields=active'),
  workers: () => net(
    fetch('/api/workers').then((r) => j(r, '/api/workers')), '/api/workers'),
  workerDetail: (name) => net(
    fetch(`/api/workers/${encodeURIComponent(name)}`)
      .then((r) => j(r, '/api/workers/:name')),
    '/api/workers/:name'),
  settings: () => net(
    fetch('/api/settings').then((r) => j(r, '/api/settings')), '/api/settings'),
  pixelStatus: () => net(
    fetch('/api/pixel/status').then((r) => j(r, '/api/pixel/status')),
    '/api/pixel/status'),
  saveSettings: (body) =>
    fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j),
  saveWorkers: (body) =>
    fetch('/api/workers', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j),
  deleteWorker: (name) =>
    fetch(`/api/workers/${encodeURIComponent(name)}`, { method: 'DELETE' }).then(j),
  resume: (task_id, instruction) =>
    fetch('/api/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id, instruction }),
    }).then(j),
  cancelTask: (task_id) =>
    fetch('/api/task/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id }),
    }).then(j),
  archiveTask: (task_id) =>
    fetch('/api/task/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id }),
    }).then(j),
  genCompose: (body) =>
    fetch('/api/compose/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j),
  // Team Work 启动器 (Step4 接真实端点; 当前 stub)
  // issue #75 s9 M-1: net() 包在最内层 —— 下方 .catch() 的 fail-soft 兜底
  // 语义逐字不变 (仍返回 stub), 但错误对象已是带 endpoint 的 ApiError。
  workflows: () => net(
    fetch('/api/workflows').then((r) => j(r, '/api/workflows')),
    '/api/workflows').catch(() => ({
    // Stub fallback: 后端尚未实现时,前端不阻塞
    // [en] E2 fix: description routed through t() (locale-aware stub copy).
    workflows: {
      'auto': { name: 'Auto-select',
                description: i18next.t('workflows.auto_description') },
    },
  })),
  teamWorkLaunch: (body) =>
    fetch('/api/team-work/launch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j),
  // ★ T5: Team Works 顶层对象视图 (UI 顶部 ACTIVE 状态卡 + 列表 + 关闭按钮)
  teamWorks: (includeClosed = false, includeArchived = false) => net(
    fetch(`/api/team-works?include_closed=${includeClosed ? 1 : 0}`
        + `&include_archived=${includeArchived ? 1 : 0}`)
      .then((r) => j(r, '/api/team-works')),
    '/api/team-works'),
  teamWorkGet: (runId) => net(
    fetch(`/api/team-work/${encodeURIComponent(runId)}`)
      .then((r) => j(r, '/api/team-work/:runId')),
    '/api/team-work/:runId'),
  // 2026-10-11: 只读 TODO.md 视图 (UI 看板『查看任务计划』), run.project_workspace 权威源。
  // 永远 fail-soft: 后端 ok=true 但 exists=false 时也返回响应对象, 不抛。
  teamWorkTodo: async (runId) => {
    try {
      return await net(
        fetch(`/api/team-work/${encodeURIComponent(runId)}/todo`)
          .then((r) => j(r, '/api/team-work/:runId/todo')),
        '/api/team-work/:runId/todo')
    } catch (e) {
      // issue #75 s9 M-1: msg 走 fmtApiError —— 断网时带归因文案 + 端点
      // (弹窗 fail-soft 文本消费 reason, TasksBoardView.jsx:1178)。
      return { ok: false, exists: false, run_id: runId,
               reason: i18next.t('common.error_loading_with_msg',
                                 { msg: fmtApiError(e) }) }
    }
  },
  teamWorkClose: (runId, body) =>
    fetch(`/api/team-work/${encodeURIComponent(runId)}/close`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || { operator: 'console' }),
    }).then(j),
  // 2026-10-06:归档非 ACTIVE run (列表收缩用)。ACTIVE 后端拒 409。
  teamWorkArchive: (runId, body) =>
    fetch(`/api/team-work/${encodeURIComponent(runId)}/archive`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || { operator: 'console' }),
    }).then(j),
  teamWorkAdvance: (runId, body) =>
    fetch(`/api/team-work/${encodeURIComponent(runId)}/advance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(j),
  // 工作目录: 列 /share_workspace/projects/ 下所有目录 + 新建
  // issue #75 s9 M-1: net() 在内层, fail-soft .catch() 语义不变。
  workspaces: () => net(
    fetch('/api/workspaces').then((r) => j(r, '/api/workspaces')),
    '/api/workspaces').catch(() => ({
    workspaces: [], root: '/share_workspace/projects', exists: false,
  })),
  workspaceCreate: (name) =>
    fetch('/api/workspaces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }).then(j),
  workflowSave: (id, body) =>
    fetch('/api/workflows/' + encodeURIComponent(id), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j),
  workflowDelete: (id) =>
    fetch('/api/workflows/' + encodeURIComponent(id), {
      method: 'DELETE',
    }).then(j),

  // 初始化 worker SOUL: 走 worker 网关的 chat,worker 收到后自己写 /opt/data/SOUL.md
  workerSoulInit: (name, content) =>
    fetch('/api/workers/' + encodeURIComponent(name) + '/soul', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    }).then(j),
  soulTemplate: () => net(
    fetch('/api/soul-template').then((r) => j(r, '/api/soul-template')),
    '/api/soul-template'),

  // ──────────────────────────────────────────────────────────────────
  // Issue #46 (s4-impl, ARCH-FILEBROWSER §4.5): 文件浏览器 fs 接口
  //   - FsHttpError 只带 status, 不解析 body (UX 约束 3 + AC-G2 零回显)
  //   - AbortSignal 透传, FilePreview 切换选择时 abort 上一次请求
  // ──────────────────────────────────────────────────────────────────
}

// 文件浏览器专用错误: 只带 status, 不解析响应 body。
// FilePreview 把 status 映射到 chat.files.err_* 文案 (UX §6)。
export class FsHttpError extends Error {
  constructor(status) {
    super(`fs http ${status}`)
    this.name = 'FsHttpError'
    this.status = status
  }
}

// 通用 GET 包装: 抛 FsHttpError(status); 支持 AbortSignal
const fsGet = async (url, signal) => {
  const r = await fetch(url, { signal })
  if (!r.ok) throw new FsHttpError(r.status)
  return r
}
// 内部把 fs 方法挂回 api 对象, 避免上方 api 字面量结构被破坏。
api.fsList = (project, path = '', signal) =>
  fsGet(`/api/fs/list?project=${encodeURIComponent(project)}`
      + `&path=${encodeURIComponent(path)}`, signal).then((r) => r.json())
api.fsFileUrl = (project, path, mode = 'embed') =>
  `/api/fs/file?project=${encodeURIComponent(project)}`
  + `&path=${encodeURIComponent(path)}&mode=${encodeURIComponent(mode)}`
api.fsText = async (project, path, signal) => {
  const r = await fsGet(api.fsFileUrl(project, path, 'embed'), signal)
  return r.text()
}
