// issue #232: 语音创建任务 — 长按录音组件 (UX-PROTOTYPE §3.2-§3.7)。
//
// 状态子集: S2 待机 / S3 录音中 / S4 转写中 / S6 错误条 (S1/S5/S7 归 ChatView)。
// 采集: Web Audio API 采 PCM → 浏览器内封 WAV (外部 ASR 只收 WAV PCM,
// PROPOSAL-V2 §2.5 实测 webm/ogg 400 拒收) → POST /api/audio/transcribe
// (mw 同源代理, 浏览器从不直连 ASR)。
//
// 交互裁决落位: D1 长按 hold-to-talk (pointerdown/up + touch-action:none);
// D4 <0.5s 松手 = 误触静默丢弃; D5 上移 >40px = 松开取消预备, 松手丢弃;
// D6 转写中经 onBusyChange 让 ChatView 禁用提交; §1.3 护栏 50s 提示 / 60s 截止。
// 文案全部走 chat.team_work_modal.voice.* (UX-PROTOTYPE §4 定稿 16 条)。
import React, { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

const MAX_MS = 60000   // 60s 自动截止 (§1.3 护栏)
const WARN_MS = 50000  // 50s 临近上限提示
const MIN_MS = 500     // <0.5s 误触静默丢弃 (D4)
const CANCEL_PX = 40   // 上移 >40px 取消预备 (D5)
const TARGET_RATE = 16000 // WAV 目标采样率 (16bit 单声道, 60s≈1.9MB)

// Float32 采样 → 16-bit PCM 单声道 WAV (纯 JS, PROPOSAL-V2 §1.1「浏览器内封 WAV」)
function encodeWav(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buf)
  const ws = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)) }
  ws(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); ws(8, 'WAVE')
  ws(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true)
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  ws(36, 'data'); view.setUint32(40, samples.length * 2, true)
  let o = 44
  for (let i = 0; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Uint8Array(buf)
}

function bytesToDataUrl(bytes) {
  let bin = ''
  const CHUNK = 0x8000 // 分块避免 String.fromCharCode 参数上限
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK))
  }
  return 'data:audio/wav;base64,' + btoa(bin)
}

const fmtSecs = (ms) => {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

const MicIcon = () => (
  <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="9" y="2" width="6" height="12" rx="3" />
    <path d="M5 10a7 7 0 0 0 14 0" />
    <path d="M12 17v4" />
  </svg>
)

export default function VoiceRecorder({ onTranscribed, onBusyChange, onUseText, onProbeRefresh }) {
  const { t } = useTranslation()
  const [phase, setPhase] = useState('idle')      // idle(S2) | rec(S3) | transcribing(S4)
  const [elapsed, setElapsed] = useState(0)
  const [warn, setWarn] = useState(false)         // 50s 临近上限
  const [cancelArmed, setCancelArmed] = useState(false)
  const [errKind, setErrKind] = useState('')      // '' | empty | down | generic | insecure | denied | nomic (S6 子态)

  const streamRef = useRef(null)
  const ctxRef = useRef(null)
  const procRef = useRef(null)
  const chunksRef = useRef([])
  const rateRef = useRef(TARGET_RATE)
  const startTsRef = useRef(0)
  const timerRef = useRef(0)
  const downYRef = useRef(0)
  const phaseRef = useRef('idle')
  phaseRef.current = phase

  // 释放全部录音资源 (松手/取消/截止/卸载共用; 幂等)
  const stopCapture = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = 0 }
    if (procRef.current) { try { procRef.current.disconnect() } catch (e) { /* noop */ } procRef.current = null }
    if (streamRef.current) { streamRef.current.getTracks().forEach((tr) => tr.stop()); streamRef.current = null }
    if (ctxRef.current) { ctxRef.current.close().catch(() => {}); ctxRef.current = null }
  }
  useEffect(() => stopCapture, []) // 卸载兜底 (弹窗 Esc/遮罩关闭 = 录音静默丢弃, §2.2)

  const fail = (kind) => {
    setErrKind(kind)
    setPhase('idle')
    setElapsed(0); setWarn(false); setCancelArmed(false)
    if (onBusyChange) onBusyChange(false)
    // §3.3: 服务不可用时前端强制重探能力 (转写失败双保险)
    if (kind === 'down' && onProbeRefresh) onProbeRefresh(true)
  }

  const send = async (dataUrl) => {
    setPhase('transcribing')
    if (onBusyChange) onBusyChange(true)
    let kind = 'generic'
    try {
      const res = await fetch('/api/audio/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data_url: dataUrl, mime_type: 'audio/wav' }),
      })
      const d = await res.json().catch(() => ({}))
      if (res.status === 503 || d.error === 'asr_unreachable') kind = 'down'
      else if (res.ok && d.ok) {
        const text = String(d.text || '').trim()
        if (text) {
          if (onBusyChange) onBusyChange(false)
          onTranscribed(text) // S5: 追加回填 + 切回文本态归 ChatView (D2)
          return
        }
        kind = 'empty' // 空转写 = 正常返回, 「未识别到语音」非报错语气 (D9)
      }
    } catch (e) {
      kind = 'generic'
    }
    fail(kind)
  }

  const finish = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = 0 }
    const chunks = chunksRef.current
    chunksRef.current = []
    const rate = rateRef.current
    const total = chunks.reduce((n, c) => n + c.length, 0)
    const pcm = new Float32Array(total)
    let off = 0
    for (const c of chunks) { pcm.set(c, off); off += c.length }
    stopCapture()
    setElapsed(0); setWarn(false); setCancelArmed(false)
    if (!total) { fail('empty'); return }
    send(bytesToDataUrl(encodeWav(pcm, rate)))
  }

  const discard = () => { // 取消/误触: 静默丢弃回 S2 (D4/D5)
    chunksRef.current = []
    stopCapture()
    setElapsed(0); setWarn(false); setCancelArmed(false)
    setPhase('idle')
  }

  const onDown = async (e) => {
    if (phaseRef.current !== 'idle') return
    // ANALYSIS.md §3 方案一: 非安全上下文 (http://IP) 浏览器禁用 getUserMedia,
    // 进入时先自检环境, 与「转写失败」分开报 (请求根本不会发出)
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) { fail('insecure'); return }
    e.preventDefault()
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch (err) { /* noop */ }
    downYRef.current = e.clientY
    setErrKind('') // §3.7: 错误条在下一次录音开始时清除
    setPhase('rec')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      let ctx
      try { ctx = new AudioContext({ sampleRate: TARGET_RATE }) }
      catch (err) { ctx = new AudioContext() }
      ctxRef.current = ctx
      rateRef.current = ctx.sampleRate || TARGET_RATE
      const src = ctx.createMediaStreamSource(stream)
      const proc = ctx.createScriptProcessor(4096, 1, 1)
      procRef.current = proc
      proc.onaudioprocess = (ev) => {
        chunksRef.current.push(new Float32Array(ev.inputBuffer.getChannelData(0)))
      }
      src.connect(proc)
      proc.connect(ctx.destination) // 输出缓冲保持静音, 仅驱动回调
    } catch (err) {
      // ANALYSIS.md §3 方案一: 按 err.name 细分 — 权限被拒/无麦克风设备/其余 → S6c
      const name = err && err.name
      if (name === 'NotAllowedError') fail('denied')
      else if (name === 'NotFoundError' || name === 'DevicesNotFoundError') fail('nomic')
      else fail('generic')
      return
    }
    startTsRef.current = Date.now()
    timerRef.current = setInterval(() => {
      const ms = Date.now() - startTsRef.current
      setElapsed(ms)
      if (ms >= WARN_MS) setWarn(true)
      if (ms >= MAX_MS) finish() // 60s 自动截止, 等同松手 (§2.2)
    }, 100)
  }

  const onMove = (e) => {
    if (phaseRef.current !== 'rec') return
    setCancelArmed(downYRef.current - e.clientY > CANCEL_PX) // 上移 >40px (D5)
  }

  const onUp = () => {
    if (phaseRef.current !== 'rec') return
    if (cancelArmed) { discard(); return }               // 松开取消 → 丢弃回 S2
    if (Date.now() - startTsRef.current < MIN_MS) { discard(); return } // 误触静默丢弃 (D4)
    finish()
  }

  const errText = errKind === 'empty' ? t('chat.team_work_modal.voice.err_empty')
    : errKind === 'down' ? t('chat.team_work_modal.voice.err_down')
    : errKind === 'insecure' ? t('chat.team_work_modal.voice.err_insecure')
    : errKind === 'denied' ? t('chat.team_work_modal.voice.err_denied')
    : errKind === 'nomic' ? t('chat.team_work_modal.voice.err_nomic')
    : errKind === 'generic' ? t('chat.team_work_modal.voice.err_generic') : ''

  return (
    <div className="flex flex-col items-center gap-3 select-none">
      {/* S3 计时 (22px 加粗 tabular-nums, §3.3) */}
      {phase === 'rec' && (
        <div className="text-[22px] font-bold text-ink tabular-nums leading-none">
          {fmtSecs(elapsed)}
        </div>
      )}
      {phase === 'transcribing' ? (
        // S4: spinner + 转写中 (沿用全局 .spin), 提交禁用由 onBusyChange 联动 (D6)
        <div className="flex flex-col items-center gap-2 py-6 text-dim text-[12.5px]">
          <span className="spin !w-[22px] !h-[22px]" />
          {t('chat.team_work_modal.voice.transcribing')}
        </div>
      ) : (
        <button type="button"
          className={`sas-voice-btn relative w-24 h-24 rounded-full flex items-center justify-center
                      text-white transition-transform duration-150 touch-none
                      ${phase === 'rec'
                        ? (cancelArmed ? 'bg-dim scale-95' : 'bg-bad scale-105 sas-voice-pulse')
                        : 'bg-accent hover:brightness-110'}`}
          style={{ touchAction: 'none' }}
          aria-label={phase === 'rec'
            ? t('chat.team_work_modal.voice.rec_hint')
            : t('chat.team_work_modal.voice.hold_hint')}
          onPointerDown={onDown} onPointerMove={onMove}
          onPointerUp={onUp} onPointerCancel={onUp}
          onContextMenu={(e) => e.preventDefault()}>
          <MicIcon />
        </button>
      )}
      {phase === 'rec' && (
        <div className={`text-[12px] ${cancelArmed ? 'text-bad' : warn ? 'text-warn' : 'text-dim'}`}>
          {cancelArmed ? t('chat.team_work_modal.voice.rec_cancel')
            : warn ? t('chat.team_work_modal.voice.rec_limit')
            : t('chat.team_work_modal.voice.rec_hint')}
        </div>
      )}
      {phase === 'idle' && !errKind && (
        <div className="text-[12px] text-dim">{t('chat.team_work_modal.voice.hold_hint')}</div>
      )}
      {/* S6 错误条: 内嵌弹窗而非 toast (D8), 承载「改用文本」出口 */}
      {errKind && (
        <div className="w-full flex items-center gap-2 rounded-lg border border-bad/40 bg-bad/8
                        px-3 py-2 text-[12.5px] text-bad">
          <span className="flex-1">{errText}</span>
          <button type="button" className="btn-ghost btn-sm shrink-0" onClick={onUseText}>
            {t('chat.team_work_modal.voice.use_text')}
          </button>
        </div>
      )}
      {/* 录音脉冲环样式自包含 (不动全局 index.css); prefers-reduced-motion 停用 (§3.3 无障碍) */}
      <style>{`
        @keyframes sas-voice-pulse {
          0% { box-shadow: 0 0 0 0 rgb(var(--c-bad) / 0.55); }
          100% { box-shadow: 0 0 0 14px rgb(var(--c-bad) / 0); }
        }
        .sas-voice-pulse { animation: sas-voice-pulse 1.4s ease-out infinite; }
        @media (prefers-reduced-motion: reduce) {
          .sas-voice-pulse { animation: none; }
        }
      `}</style>
    </div>
  )
}
