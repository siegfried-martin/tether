import { useCallback, useEffect, useState } from 'react'
import { get, patch, post, Unauthorized, type Account, type Item, type Overview, type SyncRow, type SyncSettings } from './api'

const REFRESH_MS = 60_000

function relativeTime(iso: string | null): string {
  if (!iso) return 'never'
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return 'now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function useRoute(): [string, (path: string) => void] {
  const [path, setPath] = useState(location.pathname)
  useEffect(() => {
    const onPop = () => setPath(location.pathname)
    addEventListener('popstate', onPop)
    return () => removeEventListener('popstate', onPop)
  }, [])
  const navigate = (to: string) => {
    history.pushState(null, '', to)
    setPath(to)
  }
  return [path, navigate]
}

export default function App() {
  const [me, setMe] = useState<string | null | undefined>(undefined)
  const [path, navigate] = useRoute()

  useEffect(() => {
    get<{ email: string }>('/api/me')
      .then((r) => setMe(r.email))
      .catch(() => setMe(null))
  }, [])

  if (me === undefined) return <main className="center muted">Loading…</main>
  if (me === null) return <Login />

  return (
    <div className="app">
      <header className="bar">
        <a href="/" className="brand" onClick={(e) => { e.preventDefault(); navigate('/') }}>Tether</a>
        <nav>
          <a href="/settings" className={path === '/settings' ? 'active' : ''}
             onClick={(e) => { e.preventDefault(); navigate('/settings') }}>Settings</a>
        </nav>
      </header>
      {path === '/settings' ? <Settings onSignedOut={() => setMe(null)} /> : <Home />}
    </div>
  )
}

function Login() {
  return (
    <main className="center login">
      <h1>Tether</h1>
      <a className="button" href="/auth/login">Sign in with Google</a>
    </main>
  )
}

function usePolling<T>(path: string): { data: T | null; error: string | null; reload: () => void } {
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

function Home() {
  const overview = usePolling<Overview>('/api/overview')
  const inbox = usePolling<Item[]>('/api/items?state=open&limit=200')

  return (
    <main>
      {overview.data && overview.data.urgent.length > 0 && (
        <section className="urgent" aria-label="Urgent">
          {overview.data.urgent.map((a) => (
            <div key={`${a.kind}-${a.account}`} className="alert">{a.message}</div>
          ))}
        </section>
      )}
      <section>
        <h2>Inbox {inbox.data && <span className="count">{inbox.data.length}</span>}</h2>
        {inbox.error && <p className="error">Couldn't load the inbox: {inbox.error}</p>}
        {inbox.data?.length === 0 && <p className="muted">Nothing open.</p>}
        <ul className="items">
          {inbox.data?.map((item) => <ItemRow key={item.id} item={item} />)}
        </ul>
      </section>
    </main>
  )
}

function ItemRow({ item }: { item: Item }) {
  const body = (
    <>
      <span className="swatch" style={{ background: item.account_color }} title={item.account} />
      <span className="item-main">
        <span className="sender">{item.last_direction === 'out' ? '↗ ' : ''}{item.sender || item.account}</span>
        <span className="subject">
          <span className="title">{item.title}</span>
          {item.snippet && <span className="snippet">{item.snippet}</span>}
        </span>
        <time dateTime={item.last_activity_at}>{relativeTime(item.last_activity_at)}</time>
      </span>
    </>
  )
  return (
    <li>
      {item.url ? <a className="item" href={item.url} target="_blank" rel="noreferrer">{body}</a>
                : <div className="item">{body}</div>}
    </li>
  )
}

function Settings({ onSignedOut }: { onSignedOut: () => void }) {
  const accounts = usePolling<Account[]>('/api/accounts')
  const sync = usePolling<SyncRow[]>('/api/sync-status')

  return (
    <main>
      <section>
        <h2>Accounts</h2>
        <ul className="accounts">
          {accounts.data?.map((a) => <AccountRow key={a.id} account={a} onSaved={accounts.reload} />)}
        </ul>
        {accounts.data?.length === 0 && <p className="muted">No accounts yet.</p>}
        <a className="button" href="/accounts/google/connect">Connect a Google account</a>
      </section>
      <section>
        <h2>Sync</h2>
        <table className="sync">
          <thead><tr><th>Account</th><th>Stream</th><th>Last success</th><th>Failures</th></tr></thead>
          <tbody>
            {sync.data?.filter((r) => r.stream).map((r) => (
              <tr key={`${r.account}-${r.stream}`}>
                <td>{r.account}</td>
                <td>{r.stream}</td>
                <td>{relativeTime(r.last_ok_at)}</td>
                <td title={r.last_error ?? ''}>{r.failures}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section>
        <button className="button secondary" onClick={() => post('/auth/logout').then(onSignedOut)}>Sign out</button>
      </section>
    </main>
  )
}

const CATEGORY_LABELS: Record<string, string> = {
  personal: 'Primary',
  updates: 'Updates',
  promotions: 'Promotions',
  social: 'Social',
  forums: 'Forums',
}

function AccountRow({ account, onSaved }: { account: Account; onSaved: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="account">
      <div className="account-head">
        <span className="swatch" style={{ background: account.color }} />
        <span className="grow">{account.label}</span>
        <span className={`status ${account.status}`}>
          {account.status === 'ok' ? 'connected' : account.status.replace('_', ' ')}
        </span>
        {account.sync && (
          <button className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? 'Close' : 'Sync'}
          </button>
        )}
      </div>
      {open && account.sync && (
        <SyncPanel accountId={account.id} sync={account.sync} onSaved={onSaved} />
      )}
    </li>
  )
}

function SyncPanel({ accountId, sync, onSaved }: { accountId: number; sync: SyncSettings; onSaved: () => void }) {
  const [days, setDays] = useState(String(sync.initial_sync_days))
  const [excluded, setExcluded] = useState<string[]>(sync.excluded_categories)
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null)
  const [saving, setSaving] = useState(false)

  const daysNum = Number(days)
  const daysValid = Number.isInteger(daysNum) && daysNum >= 1 && daysNum <= sync.max_days
  const dirty =
    daysNum !== sync.initial_sync_days ||
    [...excluded].sort().join() !== [...sync.excluded_categories].sort().join()

  const toggle = (cat: string) =>
    setExcluded((cur) => (cur.includes(cat) ? cur.filter((c) => c !== cat) : [...cur, cat]))

  const save = async () => {
    setSaving(true)
    setMessage(null)
    try {
      const result = await patch<{ changed: boolean }>(`/api/accounts/${accountId}/sync`, {
        excluded_categories: excluded,
        initial_sync_days: daysNum,
      })
      setMessage({ text: result.changed ? 'Saved. Re-syncing this inbox now.' : 'No changes.' })
      onSaved()
    } catch (e) {
      setMessage({ text: e instanceof Error ? e.message : String(e), error: true })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="sync-panel">
      <fieldset>
        <legend>Sync these categories</legend>
        <div className="checks">
          {sync.categories.map((cat) => (
            <label key={cat}>
              <input type="checkbox" checked={!excluded.includes(cat)} onChange={() => toggle(cat)} />
              {CATEGORY_LABELS[cat] ?? cat}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="days">
        Import the last
        <input type="number" min={1} max={sync.max_days} value={days} inputMode="numeric"
               onChange={(e) => setDays(e.target.value)} aria-invalid={!daysValid} />
        days
      </label>
      <p className="hint">
        The window sets how far back Tether imports when an account is connected or these settings change.
        Older threads still appear when they get new mail. Saving re-syncs this inbox: threads in
        unchecked categories are archived in Tether only, never in Gmail.
      </p>
      <div className="actions">
        <button className="button" disabled={!dirty || !daysValid || saving} onClick={save}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {!daysValid && <span className="error">Enter 1 to {sync.max_days} days.</span>}
        {message && <span className={message.error ? 'error' : 'muted'}>{message.text}</span>}
      </div>
    </div>
  )
}
