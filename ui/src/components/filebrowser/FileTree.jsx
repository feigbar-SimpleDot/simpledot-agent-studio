// FileTree.jsx — 懒加载树轨 (issue #46, s3-arch §4.1)
// 设计依据: docs/UX-FILEBROWSER.md §5 + docs/ARCH-FILEBROWSER.md §4.1
import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api, FsHttpError } from '../../api.js'
import { dotClass } from './kinds.js'

// 单条目 (递归)
// 注: 父容器传 expandedSet / loadingSet / childrenMap, 是为避免同层同名目录互相污染 (R-1 修复方向)。
// 展开/加载态按 (父目录路径, basename) 组成的 full path 键控 (父目录 + '/' + name)。
function TreeEntry({ entry, depth, onPick, onToggle, expandedSet, loadingSet,
                    childrenMap, selectedPath, expandedPath, truncatedMap }) {
  const { t } = useTranslation()
  const indent = { paddingLeft: `${depth * 14 + 8}px` }
  const isDir = entry.dir
  // entry.path = rel 路径 (来自 list 响应的 path+name); 子项的展开/加载按父+basename 键控
  const parent = entry.path && entry.path.includes('/')
    ? entry.path.slice(0, entry.path.lastIndexOf('/'))
    : ''
  const key = parent ? `${parent}/${entry.name}` : entry.name
  const isExpanded = expandedPath === key || expandedSet.has(key)
  const isLoading = loadingSet.has(key)
  const childList = childrenMap[key] || []
  return (
    <div>
      <button
        type="button"
        onClick={() => isDir ? onToggle(entry, key) : onPick(entry)}
        className={`w-full text-left flex items-center gap-1 py-1 pr-2 text-[12.5px]
                    hover:bg-panel2 rounded-md transition-colors
                    ${selectedPath === entry.path ? 'bg-accent/10 text-accent' : 'text-ink'}`}
        style={indent}
        title={entry.name}
        aria-expanded={isDir ? isExpanded : undefined}>
        {isDir ? (
          <span className={`w-3 h-3 shrink-0 transition-transform
                            ${isExpanded ? 'rotate-90' : ''}`}
                aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 6l6 6-6 6" />
            </svg>
          </span>
        ) : (
          <span className="w-3 h-3 shrink-0" />
        )}
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass(entry.name)}`} />
        <span className="truncate font-mono text-[12px]">{entry.name}</span>
        {entry.link && (
          <span className="ml-auto text-[10px] text-dim shrink-0 pl-2">
            {t('chat.files.symlink_note')}
          </span>
        )}
      </button>
      {isDir && isExpanded && (
        <div>
          {isLoading && (
            <div className="space-y-1 py-1" aria-busy="true">
              <div className="skeleton h-3 rounded" style={indent} />
              <div className="skeleton h-3 rounded" style={indent} />
              <div className="skeleton h-3 rounded" style={indent} />
            </div>
          )}
          {!isLoading && childList.length === 0 && (
            <div className="text-[11px] text-dim italic py-1"
                 style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}>
              {t('chat.files.empty_dir')}
            </div>
          )}
          {/* P1-1 修复: 子层截断提示 (接口 truncated=true 时同样渲染, 与根层同文案) */}
          {!isLoading && truncatedMap[key] && (
            <div className="py-1 text-[10.5px] text-warn"
                 style={{ paddingLeft: `${(depth + 1) * 14 + 8}px` }}>
              {t('chat.files.truncated', { count: 1000 })}
            </div>
          )}
          {!isLoading && childList.map((c) => (
            <TreeEntry key={c.path}
                       entry={c}
                       depth={depth + 1}
                       onPick={onPick}
                       onToggle={onToggle}
                       expandedSet={expandedSet}
                       loadingSet={loadingSet}
                       childrenMap={childrenMap}
                       selectedPath={selectedPath}
                       truncatedMap={truncatedMap}
                       expandedPath={isExpanded ? key : null} />
          ))}
        </div>
      )}
    </div>
  )
}

// 树主体
export function FileTree({ project, revealPath, onPick, refreshKey }) {
  const { t } = useTranslation()
  // items: 根层条目, 每条带 .path (rel) 用于 TreeEntry 键控
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [errKind, setErrKind] = useState(null)
  // 展开/加载态按 (parent + '/' + name) 键控, 避免同名目录互相污染
  const [expandedSet, setExpanded] = useState(() => new Set())
  const [loadingSet, setLoadingSet] = useState(() => new Set())
  const [childrenMap, setChildrenMap] = useState({})
  // P1-1: 子层截断标记 (key -> bool), 由 list 响应 truncated 字段填充
  const [truncatedMap, setTruncatedMap] = useState({})
  const [truncated, setTruncated] = useState(false)
  const [selectedPath, setSelectedPath] = useState(null)
  const abortRef = useRef(null)

  // 加载根层
  const loadRoot = useCallback(async () => {
    if (abortRef.current) abortRef.current.abort()
    const ac = new AbortController()
    abortRef.current = ac
    setLoading(true)
    setErrKind(null)
    try {
      const data = await api.fsList(project, '', ac.signal)
      // 把根层条目的 path 设为 basename, 后续键控 key = entry.name
      const rootItems = (data.items || []).map((it) => ({
        ...it,
        path: it.name,
      }))
      setItems(rootItems)
      setTruncated(!!data.truncated)
      setExpanded(new Set())
      setChildrenMap({})
      setTruncatedMap({})
      setLoadingSet(new Set())
    } catch (e) {
      if (e?.name === 'AbortError') return
      if (e instanceof FsHttpError) {
        setErrKind(e.status === 404 ? 404 : e.status === 403 ? 403 : 500)
      } else {
        setErrKind('network')
      }
    } finally {
      setLoading(false)
    }
  }, [project])

  useEffect(() => {
    loadRoot()
    return () => abortRef.current?.abort()
  }, [loadRoot, refreshKey])

  // reveal: 逐级展开到目标路径, 然后选中 (ADR-3 无 stat 流程)
  useEffect(() => {
    if (!revealPath) return
    const parts = revealPath.split('/').filter(Boolean)
    let acc = ''
    let cancelled = false
    ;(async () => {
      for (let i = 0; i < parts.length; i++) {
        if (cancelled) return
        const name = parts[i]
        const parent = acc || ''
        const key = parent ? `${parent}/${name}` : name
        setExpanded((s) => new Set([...s, key]))
        if (i < parts.length - 1) {
          // 中间段必是目录
          try {
            const data = await api.fsList(project, parts.slice(0, i + 1).join('/'))
            if (cancelled) return
            // 给子条目补 .path = parent/name
            const sub = (data.items || []).map((it) => ({
              ...it,
              path: key ? `${key}/${it.name}` : it.name,
            }))
            setChildrenMap((m) => ({ ...m, [key]: sub }))
            setTruncatedMap((m) => ({ ...m, [key]: !!data.truncated }))
          } catch {
            return
          }
        }
        acc = acc ? `${acc}/${name}` : name
      }
      if (!cancelled) {
        setSelectedPath(parts.join('/'))
        // 回调外层选中
        onPick && onPick({
          name: parts[parts.length - 1],
          path: parts.join('/'),
          dir: false,
        })
      }
    })()
    return () => { cancelled = true }
  }, [revealPath])  // eslint-disable-line react-hooks/exhaustive-deps

  // 展开/折叠: dir (entry 必带 .path, key = entry.path 即父+basename)
  const onToggle = useCallback(async (entry, key) => {
    if (entry.link) return  // symlink 不可进入 (OQ-3 UX 案)
    const k = key || entry.path || entry.name
    setExpanded((s) => {
      const ns = new Set(s)
      if (ns.has(k)) ns.delete(k)
      else ns.add(k)
      return ns
    })
    if (!childrenMap[k] && !loadingSet.has(k)) {
      setLoadingSet((s) => new Set([...s, k]))
      try {
        const data = await api.fsList(project, k)
        const sub = (data.items || []).map((it) => ({
          ...it,
          path: k ? `${k}/${it.name}` : it.name,
        }))
        setChildrenMap((m) => ({ ...m, [k]: sub }))
        setTruncatedMap((m) => ({ ...m, [k]: !!data.truncated }))
      } catch {
        // 不弹错, 折叠回去
        setExpanded((s) => {
          const ns = new Set(s); ns.delete(k); return ns
        })
      } finally {
        setLoadingSet((s) => {
          const ns = new Set(s); ns.delete(k); return ns
        })
      }
    }
  }, [project, childrenMap, loadingSet])

  if (errKind) {
    return (
      <div className="p-3 text-[12px] text-bad">
        {errKind === 403 ? t('chat.files.err_forbidden')
          : errKind === 404 ? t('chat.files.err_not_found')
          : errKind === 'network' ? t('chat.files.err_network')
          : t('chat.files.err_server')}
      </div>
    )
  }
  if (loading && items.length === 0) {
    return (
      <div className="space-y-1 p-2" aria-busy="true">
        <div className="skeleton h-4 rounded" />
        <div className="skeleton h-4 rounded w-3/4" />
        <div className="skeleton h-4 rounded w-5/6" />
      </div>
    )
  }
  if (items.length === 0) {
    return (
      <div className="p-3 text-[12px] text-dim italic">
        {t('chat.files.empty_project')}
      </div>
    )
  }
  return (
    <div className="overflow-y-auto h-full py-1">
      {truncated && (
        <div className="px-3 py-1 text-[10.5px] text-warn border-b border-line/50">
          {t('chat.files.truncated', { count: 1000 })}
        </div>
      )}
      {items.map((it) => (
        <TreeEntry key={it.path}
                   entry={it}
                   depth={0}
                   onPick={(e) => {
                     if (e.link) return
                     setSelectedPath(e.path)
                     onPick && onPick(e)
                   }}
                   onToggle={onToggle}
                   expandedSet={expandedSet}
                   loadingSet={loadingSet}
                   childrenMap={childrenMap}
                   selectedPath={selectedPath}
                   truncatedMap={truncatedMap}
                   expandedPath={null} />
      ))}
    </div>
  )
}