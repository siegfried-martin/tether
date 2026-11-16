import { useEffect, useState } from 'react'

/**
 * On a phone or tablet in the browser, a strip at the top offers to install Tether
 * as an app. Android browsers give us an install prompt to trigger; iOS has none, so
 * there it explains the Share menu. Hidden once installed, or once dismissed.
 */

const DISMISS_KEY = 'tether.install.dismissed'

type InstallPrompt = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> }

let deferred: InstallPrompt | null = null
let installed = false
const listeners = new Set<() => void>()
const notify = () => listeners.forEach((l) => l())

// Registered at load: the browser can fire this before React has rendered anything.
addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault()   // our strip replaces the browser's own mini-infobar
  deferred = e as InstallPrompt
  notify()
})
addEventListener('appinstalled', () => { deferred = null; installed = true; notify() })

function standalone(): boolean {
  return matchMedia('(display-mode: standalone)').matches
    || (navigator as Navigator & { standalone?: boolean }).standalone === true
}

function platform(): 'ios' | 'android' | 'other' {
  const ua = navigator.userAgent
  if (/iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return 'ios'
  if (/Android/.test(ua)) return 'android'
  return 'other'
}

function dismissed(): boolean {
  try { return localStorage.getItem(DISMISS_KEY) === '1' } catch { return false }
}

export function InstallBanner() {
  const [, rerender] = useState(0)
  const [hidden, setHidden] = useState(dismissed)
  const [waited, setWaited] = useState(false)
  useEffect(() => {
    const l = () => rerender((n) => n + 1)
    listeners.add(l)
    // Give the browser a moment to offer its prompt before falling back to instructions.
    const timer = setTimeout(() => setWaited(true), 2500)
    return () => { listeners.delete(l); clearTimeout(timer) }
  }, [])

  const os = platform()
  if (hidden || installed || standalone() || os === 'other') return null
  if (os === 'android' && !deferred && !waited) return null

  const close = () => {
    try { localStorage.setItem(DISMISS_KEY, '1') } catch { /* private mode */ }
    setHidden(true)
  }
  const install = async () => {
    if (!deferred) return
    const prompt = deferred
    deferred = null
    await prompt.prompt()
    const { outcome } = await prompt.userChoice
    if (outcome === 'accepted') installed = true
    rerender((n) => n + 1)
  }

  return (
    <div className="install-strip" role="region" aria-label="Install Tether">
      <img src="/icon.svg" alt="" width="28" height="28" />
      {deferred ? (
        <>
          <span>Get Tether on your home screen.</span>
          <button className="button small" onClick={install}>Install app</button>
        </>
      ) : os === 'ios' ? (
        <span>To install Tether, tap <strong>Share</strong> <ShareIcon /> then <strong>Add to Home Screen</strong>.</span>
      ) : (
        <span>To install Tether, open the browser menu <strong>⋮</strong> and choose <strong>Install app</strong> or <strong>Add to Home screen</strong>.</span>
      )}
      <button className="install-close" aria-label="Dismiss" onClick={close}>×</button>
    </div>
  )
}

const ShareIcon = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" aria-label="the Share icon" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: '-3px' }}>
    <path d="M12 3v12" /><path d="M8 7l4-4 4 4" /><path d="M6 11H5v10h14V11h-1" />
  </svg>
)
