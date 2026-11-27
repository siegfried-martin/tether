import { useEffect, useState } from 'react'
import { get, post } from './api'
import { navigate } from './route'
import { errorText } from './shared'

type PushConfig = { key: string | null; default_minutes: number }
type Device = { mail: boolean; events: boolean }

const supported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window

async function currentSubscription(): Promise<PushSubscription | null> {
  if (!supported()) return null
  const reg = await navigator.serviceWorker.getRegistration()
  return reg ? reg.pushManager.getSubscription() : null
}

function keyBytes(b64: string): Uint8Array<ArrayBuffer> {
  const raw = atob(b64.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (b64.length % 4)) % 4))
  return Uint8Array.from(raw, (c) => c.charCodeAt(0))
}

/** After sign-in: tell the server this device's current push keys (a browser may renew them),
 * and open the page a tapped notification points at. */
export function useNotifications() {
  useEffect(() => {
    if (!supported()) return
    currentSubscription().then((sub) => sub && post('/api/push/subscription', sub.toJSON())).catch(() => {})
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'open' && typeof e.data.url === 'string') navigate(e.data.url)
    }
    navigator.serviceWorker.addEventListener('message', onMessage)
    return () => navigator.serviceWorker.removeEventListener('message', onMessage)
  }, [])
}

/** Settings: notifications on this device, for new mail and for event reminders. */
export function NotificationSettings() {
  const [config, setConfig] = useState<PushConfig | null>(null)
  const [sub, setSub] = useState<PushSubscription | null>(null)
  const [device, setDevice] = useState<Device | null>(null)
  const [permission, setPermission] = useState(supported() ? Notification.permission : 'default')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)

  useEffect(() => {
    if (!supported()) return
    get<PushConfig>('/api/push').then(setConfig, (e) => setMessage({ text: errorText(e), error: true }))
    currentSubscription().then(async (s) => {
      setSub(s)
      if (s) setDevice((await post<{ device: Device | null }>('/api/push/device', { endpoint: s.endpoint })).device)
    }).catch(() => {})
  }, [])

  const run = async (work: () => Promise<void>) => {
    setBusy(true); setMessage(null)
    try { await work() } catch (e) { setMessage({ text: errorText(e), error: true }) } finally { setBusy(false) }
  }

  const turnOn = () => run(async () => {
    const answer = await Notification.requestPermission()
    setPermission(answer)
    if (answer !== 'granted') return
    const reg = await navigator.serviceWorker.ready
    const s = (await reg.pushManager.getSubscription())
      ?? await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(config!.key!) })
    const r = await post<{ device: Device }>('/api/push/subscription', { ...s.toJSON(), mail: true, events: true })
    setSub(s); setDevice(r.device)
    setMessage({ text: 'Notifications are on for this device.' })
  })

  const choose = (change: Partial<Device>) => run(async () => {
    const r = await post<{ device: Device }>('/api/push/subscription', { ...sub!.toJSON(), ...change })
    setDevice(r.device)
  })

  const test = () => run(async () => {
    await post('/api/push/test', { endpoint: sub!.endpoint })
    setMessage({ text: 'Sent. It should show up in a few seconds.' })
  })

  const turnOff = () => run(async () => {
    await post('/api/push/unsubscribe', { endpoint: sub!.endpoint })
    await sub!.unsubscribe()
    setSub(null); setDevice(null)
    setMessage({ text: 'Notifications are off for this device.' })
  })

  let body: React.ReactNode
  if (!supported()) {
    body = <p className="muted">This browser can't show notifications from Tether.</p>
  } else if (!config) {
    body = null
  } else if (!config.key) {
    body = <p className="muted">Notifications aren't set up on the server yet.</p>
  } else if (permission === 'denied') {
    body = (
      <p className="muted">
        Notifications are blocked for Tether. In the Android app: long-press the Tether icon, then App info,
        then Notifications. In a browser: the site settings beside the address.
      </p>
    )
  } else if (!sub || !device) {
    body = (
      <div className="actions">
        <button className="button" disabled={busy} onClick={turnOn}>
          {busy ? 'Turning on…' : 'Turn on notifications on this device'}
        </button>
      </div>
    )
  } else {
    body = (
      <>
        <div className="checks">
          <label>
            <input type="checkbox" checked={device.mail} disabled={busy} onChange={() => choose({ mail: !device.mail })} />
            New email
          </label>
          <label>
            <input type="checkbox" checked={device.events} disabled={busy}
                   onChange={() => choose({ events: !device.events })} />
            Event reminders
          </label>
        </div>
        <div className="actions">
          <button className="button secondary" disabled={busy} onClick={test}>Send a test</button>
          <button className="button secondary" disabled={busy} onClick={turnOff}>Turn off on this device</button>
        </div>
      </>
    )
  }

  return (
    <section className="notify">
      <h2>Notifications</h2>
      {body}
      {message && <p className={message.error ? 'error' : 'muted'}>{message.text}</p>}
      {config?.key && (
        <p className="hint">
          New email: inbox mail as it arrives. Reminders: at each event's own notification time, or{' '}
          {config.default_minutes} minutes before when it has none (all-day events only when they set one).
          Each device chooses for itself.
        </p>
      )}
    </section>
  )
}
