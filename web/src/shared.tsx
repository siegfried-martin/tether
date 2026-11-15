import { useCallback, useEffect, useState } from 'react'
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
  const reload = useCallback(() => {
    get<T>(path)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => {
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
