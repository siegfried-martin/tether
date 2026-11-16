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

/** Whether a CSS media query matches, kept current as the screen turns or resizes. */
export function useMedia(query: string): boolean {
  const [matches, setMatches] = useState(() => matchMedia(query).matches)
  useEffect(() => {
    const list = matchMedia(query)
    const update = () => setMatches(list.matches)
    update()
    list.addEventListener('change', update)
    return () => list.removeEventListener('change', update)
  }, [query])
  return matches
}

/** Short screens (a phone on its side) get the compact calendar: its toolbar joins the header row. */
export const SHORT_SCREEN = '(max-height: 500px)'

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
/** Account colors, named so they can be picked without telling them apart. Mirrors PALETTE in services/accounts.py. */
export const PALETTE = [
  { hex: '#4377f0', name: 'Blue' }, { hex: '#b23b19', name: 'Rust' }, { hex: '#b0267c', name: 'Magenta' },
  { hex: '#48cba1', name: 'Mint' }, { hex: '#ee9e10', name: 'Amber' }, { hex: '#21c5e5', name: 'Cyan' },
  { hex: '#2e7a50', name: 'Forest' }, { hex: '#6b7280', name: 'Gray' },
]

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

/**
 * A sideways swipe on touch screens: left calls onSwipe(1) (next), right onSwipe(-1);
 * onSwipe returns false if there is nothing that way.
 * With follow, the element tracks the finger, then slides in from that side when the
 * swipe counts. Mostly-vertical drags are left to scrolling. Returns a ref callback.
 */
export function useSwipe(onSwipe: React.MutableRefObject<(dir: 1 | -1) => boolean | void>, { follow = true } = {}) {
  const [el, setEl] = useState<HTMLElement | null>(null)
  useEffect(() => {
    if (!el) return
    let x0 = 0, y0 = 0, t0 = 0, axis: 'x' | 'y' | null = null, dx = 0
    const reset = (animate: boolean) => {
      el.style.transition = animate ? 'transform 160ms ease-out, opacity 160ms ease-out' : ''
      el.style.transform = ''
      el.style.opacity = ''
    }
    const start = (e: TouchEvent) => {
      if (e.touches.length !== 1) { axis = 'y'; return }
      x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; t0 = e.timeStamp; axis = null; dx = 0
    }
    const move = (e: TouchEvent) => {
      if (axis === 'y' || e.touches.length !== 1) return
      dx = e.touches[0].clientX - x0
      const dy = e.touches[0].clientY - y0
      if (!axis) {
        if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return
        axis = Math.abs(dx) > Math.abs(dy) * 1.2 ? 'x' : 'y'
        if (axis === 'y') return
      }
      e.preventDefault()   // keep a sideways drag from scrolling or going back a page
      if (!follow) return
      el.style.transition = ''
      el.style.transform = `translateX(${dx}px)`
      el.style.opacity = String(Math.max(0.4, 1 - Math.abs(dx) / el.clientWidth))
    }
    const end = (e: TouchEvent) => {
      if (axis !== 'x') return
      axis = null
      const fast = Math.abs(dx) > 40 && e.timeStamp - t0 < 300
      if (Math.abs(dx) < el.clientWidth * 0.22 && !fast) { reset(true); return }
      const dir = dx < 0 ? 1 : -1
      // onSwipe returns false when there is nowhere to go (the first or last page): spring back.
      if (onSwipe.current(dir) === false) { reset(true); return }
      if (!follow) return
      // Jump to the far side, then glide back into place with the new content.
      el.style.transition = ''
      el.style.transform = `translateX(${dir * 40}%)`
      el.style.opacity = '0.4'
      requestAnimationFrame(() => requestAnimationFrame(() => reset(true)))
    }
    const cancel = () => { axis = null; reset(true) }
    el.addEventListener('touchstart', start, { passive: true })
    el.addEventListener('touchmove', move, { passive: false })
    el.addEventListener('touchend', end)
    el.addEventListener('touchcancel', cancel)
    return () => {
      el.removeEventListener('touchstart', start)
      el.removeEventListener('touchmove', move)
      el.removeEventListener('touchend', end)
      el.removeEventListener('touchcancel', cancel)
    }
  }, [el, onSwipe, follow])
  return setEl
}
