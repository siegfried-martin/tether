import { useEffect, useReducer } from 'react'

/**
 * A tiny router over the History API. Every page and view has its own URL, so the
 * browser's back button works, and the last place visited is remembered: opening
 * Tether at "/" goes back there.
 */

const LAST_KEY = 'tether.last'
export const PAGES = ['inbox', 'tasks', 'calendar', 'settings'] as const
export type Page = (typeof PAGES)[number]

const listeners = new Set<() => void>()
const notify = () => listeners.forEach((l) => l())

function remember() {
  if (!pageOf(location.pathname)) return
  try { localStorage.setItem(LAST_KEY, location.pathname + location.search) } catch { /* private mode */ }
}

export function lastLocation(): string {
  try {
    const saved = localStorage.getItem(LAST_KEY)
    if (saved && pageOf(saved.split('?')[0])) return saved
  } catch { /* private mode */ }
  return '/inbox'
}

export function pageOf(path: string): Page | null {
  const first = path.split('/')[1] as Page
  return PAGES.includes(first) ? first : null
}

export function navigate(to: string, { replace = false } = {}) {
  if (to === location.pathname + location.search) return
  history[replace ? 'replaceState' : 'pushState'](null, '', to)
  remember()
  if (!replace) scrollTo(0, 0)
  notify()
}

addEventListener('popstate', () => { remember(); notify() })
remember()   // a page opened directly (a bookmark, a reload) counts too

export type Location = { path: string; params: URLSearchParams }

export function useLocation(): Location {
  const [, rerender] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    listeners.add(rerender)
    return () => { listeners.delete(rerender) }
  }, [])
  return { path: location.pathname, params: new URLSearchParams(location.search) }
}

/** The current URL with some query parameters changed; null or '' removes one. */
export function withParams(changes: Record<string, string | number | null>): string {
  const params = new URLSearchParams(location.search)
  for (const [key, value] of Object.entries(changes)) {
    if (value === null || value === '') params.delete(key)
    else params.set(key, String(value))
  }
  const query = params.toString()
  return location.pathname + (query ? `?${query}` : '')
}

/** A link that navigates in-app on a plain click and still opens in a new tab on ctrl/cmd-click. */
export function linkProps(to: string) {
  return {
    href: to,
    onClick: (e: React.MouseEvent) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      navigate(to)
    },
  }
}
