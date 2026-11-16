import { useCallback, useEffect, useRef, useState } from 'react'
import { get, Unauthorized } from './api'

const REFRESH_MS = 60_000

export function relativeTime(iso: string | null): string {
  if (!iso) return 'never'
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return 'now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function usePolling<T>(path: string): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const current = useRef(path)
  current.current = path
  const reload = useCallback(() => {
    if (!path) return
    get<T>(path)
      .then((d) => { if (current.current === path) { setData(d); setError(null) } })   // drop a stale answer
      .catch((e) => {
        if (current.current !== path) return
        if (e instanceof Unauthorized) location.reload()
        else setError(String(e.message ?? e))
      })
  }, [path])
  useEffect(() => {
    reload()
    const timer = setInterval(reload, REFRESH_MS)
    const onVisible = () => document.visibilityState === 'visible' && reload()
    document.addEventListener('visibilitychange', onVisible)
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) }
  }, [reload])
  return { data, error, reload }
}

export type Toast = { text: string; error?: boolean; undo?: () => void }

export function useToast(): [Toast | null, (t: Toast | null) => void] {
  const [toast, setToast] = useState<Toast | null>(null)
  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(null), toast.error ? 8000 : 6000)
    return () => clearTimeout(timer)
  }, [toast])
  return [toast, setToast]
}

export function ToastView({ toast }: { toast: Toast | null }) {
  if (!toast) return null
  return (
    <div className={`toast${toast.error ? ' error-toast' : ''}`} role="status">
      <span>{toast.text}</span>
      {toast.undo && <button className="link" onClick={toast.undo}>Undo</button>}
    </div>
  )
}

export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e))

export const PAGE_SIZE = 50

/** The page number in the URL (1-based), clamped to at least 1. */
export function pageParam(params: URLSearchParams): number {
  const n = Number(params.get('page'))
  return Number.isInteger(n) && n > 1 ? n : 1
}

export function Pager({ page, total, onPage }: { page: number; total: number; onPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  if (total <= PAGE_SIZE && page === 1) return null
  const first = Math.min(total, (page - 1) * PAGE_SIZE + 1)
  const last = Math.min(total, page * PAGE_SIZE)
  return (
    <nav className="pager" aria-label="Pages">
      <span className="muted">{first}–{last} of {total}</span>
      <button className="pager-button" onClick={() => onPage(page - 1)} disabled={page <= 1} aria-label="Previous page">‹</button>
      <button className="pager-button" onClick={() => onPage(page + 1)} disabled={page >= pages} aria-label="Next page">›</button>
    </nav>
  )
}

/** Inline style that gives a row its account's color: a solid edge and a tinted background. */
export function accountStyle(color: string | null | undefined): React.CSSProperties {
  return { '--acct': color ?? 'var(--muted)' } as React.CSSProperties
}

const URL_RE = /(https?:\/\/[^\s<>"')\]]+[^\s<>"')\].,;:!?])/g

/** Plain text with its web links made clickable. */
export function Linkified({ text }: { text: string }) {
  const parts = text.split(URL_RE)
  return (
    <>
      {parts.map((part, n) => (n % 2 === 1
        ? <a key={n} href={part} target="_blank" rel="noreferrer noopener" className="inline-link">{part}</a>
        : part))}
    </>
  )
}

export function localToday(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T12:00:00`)
  d.setDate(d.getDate() + days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function DueLabel({ due }: { due: string | null }) {
  if (!due) return null
  const day = due.slice(0, 10)
  const today = localToday()
  const pretty = new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  if (day < today) return <span className="due overdue">Overdue · {pretty}</span>
  if (day === today) return <span className="due today">Today</span>
  if (day === addDays(today, 1)) return <span className="due">Tomorrow</span>
  return <span className="due">{pretty}</span>
}

