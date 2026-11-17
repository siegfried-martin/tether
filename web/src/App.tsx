import { useEffect, useRef, useState } from 'react'
import { get, patch, post, type Account, type Item, type ItemPage, type Overview, type SyncRow, type SyncSettings, type Task } from './api'
import { accountStyle, ColorPicker, DueLabel, useSwipe, errorText, PAGE_SIZE, Pager, pageParam, relativeTime, ToastView, usePolling, useToast } from './shared'
import { CalendarPage, CalendarSettings, TodayCard } from './Calendar'
import { TagSettings, TasksPage } from './Tasks'
import { ExternalIcon, externalName, ReadingPane } from './Mail'
import { InstallBanner } from './Install'
import { lastLocation, linkProps, navigate, pageOf, useLocation, withParams, type Location, type Page } from './route'

const NAV = [['inbox', 'Inbox'], ['tasks', 'Tasks'], ['calendar', 'Calendar'], ['settings', 'Settings']] as const

const NAV_ICONS: Record<(typeof NAV)[number][0], React.ReactNode> = {
  inbox: <><path d="M3 13h5l1.5 3h5l1.5-3h5" /><path d="M5.5 5h13L21 13v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z" /></>,
  tasks: <><rect x="3.5" y="3.5" width="17" height="17" rx="3" /><path d="M8 12.5l2.8 2.8L16 9.5" /></>,
  calendar: <><rect x="3.5" y="5" width="17" height="15.5" rx="2" /><path d="M3.5 10h17M8 3v4M16 3v4" /></>,
  settings: <><path d="M4 7h9M17 7h3M4 17h3M11 17h9" /><circle cx="15" cy="7" r="2.2" /><circle cx="9" cy="17" r="2.2" /></>,
}

export default function App() {
  const [me, setMe] = useState<string | null | undefined>(undefined)
  const loc = useLocation()
  const page = pageOf(loc.path)
  const [barSlot, setBarSlot] = useState<HTMLDivElement | null>(null)   // where a page can put controls in the header

  useEffect(() => {
    get<{ email: string }>('/api/me')
      .then((r) => setMe(r.email))
      .catch(() => setMe(null))
  }, [])
  // "/" (and anything unknown) goes back to where you were last.
  useEffect(() => { if (me && !page) navigate(lastLocation(), { replace: true }) }, [me, page])

  if (me === undefined) return <main className="center muted">Loading…</main>
  if (me === null) return <><InstallBanner /><Login /></>

  return (
    <div className="app">
      <header className="bar">
        <a className="brand" {...linkProps('/inbox')}>
          <img src="/icon.svg" alt="" width="28" height="28" /><span className="brand-name">Tether</span>
        </a>
        <div className="bar-slot" ref={setBarSlot} />
        <nav className="tabs">
          {NAV.map(([to, label]) => (
            <a key={to} className={page === to ? 'active' : ''} aria-current={page === to ? 'page' : undefined}
               {...linkProps(`/${to}`)}>
              <NavIcon page={to} />
              {label}
            </a>
          ))}
        </nav>
        {page && <PageCarousel page={page} />}
      </header>
      <InstallBanner />
      {page === 'settings' ? <Settings onSignedOut={() => setMe(null)} />
        : page === 'tasks' ? <TasksPage loc={loc} />
        : page === 'calendar' ? <CalendarPage loc={loc} barSlot={barSlot} />
        : page === 'inbox' ? <Home loc={loc} /> : null}
    </div>
  )
}

const NavIcon = ({ page, size = 20 }: { page: (typeof NAV)[number][0]; size?: number }) => (
  <svg className="nav-icon" viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" fill="none"
       stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{NAV_ICONS[page]}</svg>
)

/**
 * The page switcher on phones: the current page in the middle, its neighbours at the
 * sides, and dots for where it sits. Swipe it (or tap a side) to move one page over.
 */
function PageCarousel({ page }: { page: Page }) {
  const at = NAV.findIndex(([to]) => to === page)
  const prev = NAV[at - 1], next = NAV[at + 1]
  const go = useRef<(dir: 1 | -1) => boolean>(() => false)
  go.current = (dir) => {
    const to = NAV[at + dir]
    if (!to) return false
    navigate(`/${to[0]}`)
    return true
  }
  const swipe = useSwipe(go)
  return (
    <div className="page-carousel" ref={swipe} aria-label="Pages">
      {prev
        ? <a className="pc-side prev" {...linkProps(`/${prev[0]}`)}><span aria-hidden="true">‹</span> {prev[1]}</a>
        : <span className="pc-side" />}
      <div className="pc-current" aria-current="page">
        <span className="pc-label"><NavIcon page={page} size={18} />{NAV[at][1]}</span>
        <span className="pc-dots" aria-hidden="true">{NAV.map(([to]) => <i key={to} className={to === page ? 'on' : ''} />)}</span>
      </div>
      {next
        ? <a className="pc-side next" {...linkProps(`/${next[0]}`)}>{next[1]} <span aria-hidden="true">›</span></a>
        : <span className="pc-side" />}
    </div>
  )
}

/** The public pages Google's consent screen points to. Full page loads: they are not part of the app. */
const PolicyLinks = () => (
  <nav className="policy-links" aria-label="About Tether">
    <a href="/about">About</a><a href="/privacy.html">Privacy policy</a><a href="/terms.html">Terms of service</a>
  </nav>
)

function Login() {
  return (
    <main className="center login">
      <img src="/icon.svg" alt="" width="72" height="72" />
      <h1>Tether</h1>
      <a className="button" href="/auth/login">Sign in with Google</a>
      <PolicyLinks />
    </main>
  )
}

function Home({ loc }: { loc: Location }) {
  const tab = loc.path === '/inbox/archived' ? 'archived' : 'open'
  const account = loc.params.get('account')
  const page = pageParam(loc.params)
  const overview = usePolling<Overview>('/api/overview')
  const accountList = usePolling<Account[]>('/api/accounts')
  const query = new URLSearchParams({ state: tab, limit: String(PAGE_SIZE), offset: String((page - 1) * PAGE_SIZE) })
  if (account) query.set('account', account)
  const list = usePolling<ItemPage>(`/api/items?${query}`)
  // Rows moved out of this view, hidden at once while the server catches up.
  const [moved, setMoved] = useState<Set<number>>(new Set())
  const [reading, setReading] = useState<number | null>(null)
  const [toast, setToast] = useToast()

  const refresh = () => { list.reload(); overview.reload() }
  const hide = (id: number, on: boolean) =>
    setMoved((cur) => { const next = new Set(cur); if (on) next.add(id); else next.delete(id); return next })

  const act = async (item: Item, action: 'archive' | 'unarchive', undoable = true) => {
    hide(item.id, true)
    try {
      const result = await post<Item>(`/api/items/${item.id}/${action}`)
      const inverse = action === 'archive' ? 'unarchive' : 'archive'
      setToast(result.note ? { text: result.note }
        : { text: action === 'archive' ? 'Archived' : 'Moved to inbox',
            undo: undoable ? () => { setToast(null); act(item, inverse, false) } : undefined })
      // A row undone back into this view must show again.
      if (!undoable) hide(item.id, false)
    } catch (e) {
      hide(item.id, false)
      setToast({ text: errorText(e), error: true })
    } finally {
      refresh()
    }
  }

  const makeTask = async (item: Item) => {
    try {
      // A digest item already says what is needed, by when and how urgently.
      const fromDigest = item.kind === 'digest'
      await post<Task>('/api/tasks', {
        title: (fromDigest && item.needed) || item.title, item_id: item.id,
        due_at: fromDigest ? item.due_at : null, priority: fromDigest && item.priority === 'high' ? 'high' : null,
      })
      setToast({ text: 'Added to Tasks. Completing it archives this email.' })
    } catch (e) {
      setToast({ text: errorText(e), error: true })
    } finally {
      refresh()
    }
  }

  const completeTask = async (task: Task) => {
    try {
      const done = await post<Task & { warnings: string[] }>(`/api/tasks/${task.id}/complete`)
      setToast(done.warnings.length ? { text: done.warnings.join(' '), error: true }
        : { text: `Done: ${task.title}`, undo: () => { setToast(null); post(`/api/tasks/${task.id}/reopen`).then(refresh) } })
    } catch (e) {
      setToast({ text: errorText(e), error: true })
    } finally {
      refresh()
    }
  }

  const counts = overview.data?.inbox
  const shown = list.data?.items.filter((i) => !moved.has(i.id))
  const accountsSeen = accountList.data ?? []
  const total = counts && (account ? counts.by_account[account] ?? 0 : counts.count)
  const tabPath = (state: 'open' | 'archived') =>
    (state === 'open' ? '/inbox' : '/inbox/archived') + (account ? `?account=${encodeURIComponent(account)}` : '')
  const filterPath = (label: string | null) => withParams({ account: label, page: null })

  return (
    <main>
      {overview.data && <Urgent urgent={overview.data.urgent} onComplete={completeTask} />}
      {overview.data?.calendar && <TodayCard events={overview.data.calendar.today} />}
      <section>
        <div className="inbox-head">
          <div className="tabs" role="tablist">
            <a role="tab" aria-selected={tab === 'open'} {...linkProps(tabPath('open'))}>
              Inbox {total !== undefined && <span className="count">{total}</span>}
            </a>
            <a role="tab" aria-selected={tab === 'archived'} {...linkProps(tabPath('archived'))}>
              Archived
            </a>
          </div>
          {accountsSeen.length > 1 && (
            <div className="chips" aria-label="Filter by account">
              <a className="chip" aria-pressed={account === null} {...linkProps(filterPath(null))}>All</a>
              {accountsSeen.map((a) => (
                <a key={a.id} className="chip" aria-pressed={account === a.label} title={a.label} style={accountStyle(a.color)}
                   {...linkProps(filterPath(account === a.label ? null : a.label))}>
                  <span className="dot" />
                  {a.label}
                  {tab === 'open' && <span className="chip-count">{counts?.by_account[a.label] ?? 0}</span>}
                </a>
              ))}
            </div>
          )}
        </div>
        {list.error && <p className="error">Couldn't load the inbox: {list.error}</p>}
        {shown?.length === 0 && <p className="muted">{tab === 'open' ? 'Nothing open.' : 'Nothing archived yet.'}</p>}
        <ul className="items">
          {shown?.map((item) => (
            <ItemRow key={item.id} item={item} open={reading === item.id}
                     onToggle={() => setReading(reading === item.id ? null : item.id)}
                     onTask={() => makeTask(item)}
                     onAction={() => act(item, item.state === 'open' ? 'archive' : 'unarchive')} />
          ))}
        </ul>
        {list.data && <Pager page={page} total={list.data.total} onPage={(n) => navigate(withParams({ page: n > 1 ? n : null }))} />}
      </section>
      <ToastView toast={toast} />
    </main>
  )
}

function Urgent({ urgent, onComplete }: { urgent: Overview['urgent']; onComplete: (t: Task) => void }) {
  const empty = !urgent.alerts.length && !urgent.tasks.length && !urgent.items.length
  if (empty) return null
  return (
    <section className="urgent" aria-label="Urgent">
      {urgent.alerts.map((a) => <div key={`${a.kind}-${a.account}`} className="alert">{a.message}</div>)}
      {urgent.tasks.map((t) => (
        <div key={`t${t.id}`} className="urgent-row">
          <input type="checkbox" className="check" aria-label={`Complete: ${t.title}`} onChange={() => onComplete(t)} />
          <span className="grow">{t.title}</span>
          <DueLabel due={t.due_at} />
        </div>
      ))}
      {urgent.items.map((i) => (
        <a key={`i${i.id}`} className="urgent-row tinted" style={accountStyle(i.account_color)}
           href={i.url ?? undefined} target="_blank" rel="noreferrer">
          <span className="grow">{i.title}</span>
          {i.priority === 'high' && <span className="flag">High</span>}
          <DueLabel due={i.due_at} />
        </a>
      ))}
    </section>
  )
}

const ArchiveIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="5" rx="1" /><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9" />
    <path d="M10 13h4" />
  </svg>
)
const TaskIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="18" height="18" rx="3" /><path d="M8 12l3 3 5-6" />
  </svg>
)
const InboxIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 13h5l1.5 3h5L16 13h5" /><path d="M5 5h14l2 8v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z" />
  </svg>
)

function ItemRow({ item, open, onToggle, onAction, onTask }: {
  item: Item; open: boolean; onToggle: () => void; onAction: () => void; onTask: () => void
}) {
  const archive = item.state === 'open'
  return (
    <li className={`item-row${open ? ' open' : ''}`} style={accountStyle(item.account_color)}>
      <div className="item-line">
        <button className="item" onClick={onToggle} aria-expanded={open} title={item.account}>
          <span className="item-main">
            <span className="sender">
              {item.last_direction === 'out' ? '↗ ' : ''}{item.sender || item.account}
              {item.open_tasks > 0 && <span className="task-badge" title="Has an open task">task</span>}
            </span>
            <span className="subject">
              <span className="title">{item.title}</span>
              {item.snippet && <span className="snippet">{item.snippet}</span>}
            </span>
            <span className="item-time">
              {item.priority === 'high' && <span className="flag">High</span>}
              <DueLabel due={item.due_at} />
              <time dateTime={item.last_activity_at}>{relativeTime(item.last_activity_at)}</time>
            </span>
          </span>
        </button>
        {item.url && (
          <a className="row-action" href={item.url} target="_blank" rel="noreferrer"
             title={`Open in ${externalName(item.url)}`} aria-label={`Open in ${externalName(item.url)}: ${item.title}`}>
            <ExternalIcon />
          </a>
        )}
        {item.open_tasks === 0 && item.state === 'open' && (
          <button className="row-action" onClick={onTask} title="Make a task" aria-label={`Make a task: ${item.title}`}>
            <TaskIcon />
          </button>
        )}
        <button className="row-action" onClick={onAction}
                title={archive ? 'Archive' : 'Move to inbox'} aria-label={`${archive ? 'Archive' : 'Move to inbox'}: ${item.title}`}>
          {archive ? <ArchiveIcon /> : <InboxIcon />}
        </button>
      </div>
      {open && <ReadingPane itemId={item.id} kind={item.kind} url={item.url} digest={item} />}
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
      <CalendarSettings />
      <TagSettings />
      <DigestPanel onIngested={accounts.reload} />
      <section>
        <h2>Sync</h2>
        <table className="sync">
          <thead><tr><th>Account</th><th>Stream</th><th>Last success</th><th>Failures</th></tr></thead>
          <tbody>
            {sync.data?.filter((r) => r.stream).map((r) => (
              <tr key={`${r.account}-${r.stream}`}>
                <td>{r.account}</td>
                <td>{r.source ? `calendar: ${r.source}` : r.stream}</td>
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
      <PolicyLinks />
    </main>
  )
}

type DigestResult = {
  created: number; duplicates: number
  rejected: { block: string; reason: string }[]; warnings: { block: string; reason: string }[]
}

function DigestPanel({ onIngested }: { onIngested: () => void }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<DigestResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const out = await post<DigestResult>('/api/digest', { raw_text: text })
      setResult(out)
      if (!out.rejected.length) setText('')
      onIngested()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section>
      <h2>Work digest</h2>
      <p className="muted">
        Paste the digest from your work assistant. Items land in the inbox under the digest account. You can
        also paste it to Claude, which passes it to Tether unchanged.
      </p>
      <form className="digest-form" onSubmit={submit}>
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} aria-label="Digest text"
                  placeholder={'ITEM\nFrom: …\nSubject: …\nReceived: …\nNeeded: …\nDue: …\nPriority: …\nLink: …'} />
        <button className="button" disabled={!text.trim() || busy}>Add to inbox</button>
      </form>
      {error && <p className="error">{error}</p>}
      {result && (
        <div className="digest-result" role="status">
          <p>{result.created} new, {result.duplicates} already in Tether.</p>
          {result.rejected.map((r, n) => (
            <p key={`r${n}`} className="error">Skipped: {r.reason} <span className="muted">{r.block}</span></p>
          ))}
          {result.warnings.map((w, n) => <p key={`w${n}`} className="muted">{w.block}: {w.reason}</p>)}
        </div>
      )}
    </section>
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
        <ColorPicker value={account.color} label={`Color for ${account.label}`}
                     save={(color) => patch(`/api/accounts/${account.id}`, { color }).then(onSaved)} />
        <span className="grow">{account.label}</span>
        <span className={`status ${account.status}`}>
          {account.kind === 'digest' ? 'digest' : account.kind === 'ics' ? 'calendar feed'
            : account.status === 'ok' ? 'connected' : account.status.replace('_', ' ')}
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
