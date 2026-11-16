import { useState } from 'react'
import { patch, post, type AgendaEvent, type CalendarStatus, type Hold } from './api'
import { errorText, usePolling } from './shared'

const DAYS = 14

function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function plusDays(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00`)
  d.setDate(d.getDate() + n)
  return isoDay(d)
}

/** The local day an event starts on. All-day events carry their date as-is. */
function dayOf(e: AgendaEvent): string {
  return e.all_day ? e.start : isoDay(new Date(e.start))
}

const clock = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })

function timeRange(e: AgendaEvent): string {
  if (e.all_day) {
    const days = Math.round((new Date(`${e.end}T12:00:00`).getTime() - new Date(`${e.start}T12:00:00`).getTime()) / 864e5)
    return days > 1 ? `All day · ${days} days` : 'All day'
  }
  return e.end > e.start ? `${clock(e.start)} – ${clock(e.end)}` : clock(e.start)
}

function dayLabel(day: string, today: string): string {
  if (day === today) return 'Today'
  if (day === plusDays(today, 1)) return 'Tomorrow'
  return new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })
}

const BUSY_LABEL: Record<AgendaEvent['busy'], string | null> = {
  busy: null, tentative: 'Not answered', free: 'Free', oof: 'Out of office',
}

export function CalendarPage() {
  const today = isoDay(new Date())
  const agenda = usePolling<AgendaEvent[]>(`/api/agenda?start=${today}&end=${plusDays(today, DAYS - 1)}`)
  const byDay = new Map<string, AgendaEvent[]>()
  for (const e of agenda.data ?? []) {
    const day = dayOf(e)
    byDay.set(day, [...(byDay.get(day) ?? []), e])
  }
  return (
    <main className="calendar-page">
      {agenda.error && <p className="error">Couldn't load the calendar: {agenda.error}</p>}
      {agenda.data?.length === 0 && (
        <p className="muted">Nothing in the next two weeks. Choose which calendars to include in Settings.</p>
      )}
      {[...byDay.entries()].map(([day, events]) => (
        <section key={day} className="day">
          <h2 className={day === today ? 'today' : ''}>{dayLabel(day, today)}</h2>
          <ul className="events">
            {events.map((e) => <EventRow key={e.id} event={e} />)}
          </ul>
        </section>
      ))}
    </main>
  )
}

function EventRow({ event: e }: { event: AgendaEvent }) {
  const [open, setOpen] = useState(false)
  const past = !e.all_day && new Date(e.end) < new Date()
  const detail = e.description || e.location
  return (
    <li className={`event ${e.busy}${past ? ' past' : ''}`}>
      <span className="event-time">{timeRange(e)}</span>
      <div className="event-main">
        <button className="event-title" onClick={() => setOpen(!open)} aria-expanded={open} disabled={!detail}>
          {e.title}
        </button>
        <span className="event-meta">
          {e.sources.map((s) => <span key={s.source_id} className="dot" style={{ background: s.color }} title={s.name} />)}
          <span className="muted">{e.sources.map((s) => s.name).join(', ')}</span>
          {BUSY_LABEL[e.busy] && <span className="busy-tag">{BUSY_LABEL[e.busy]}</span>}
          {e.location && <span className="muted location">{e.location}</span>}
        </span>
        {open && e.description && <p className="event-description">{e.description}</p>}
      </div>
      {e.join_url && !past && (
        <a className="button join" href={e.join_url} target="_blank" rel="noreferrer">Join</a>
      )}
    </li>
  )
}

/** Today's events on the home screen: what's left of the day, then a link to the full calendar. */
export function TodayCard({ events, onOpen }: { events: AgendaEvent[]; onOpen: () => void }) {
  const now = new Date()
  const left = events.filter((e) => e.all_day || new Date(e.end) > now)
  return (
    <section className="today-card">
      <div className="card-head">
        <h2>Today</h2>
        <a href="/calendar" className="link" onClick={(ev) => { ev.preventDefault(); onOpen() }}>Calendar</a>
      </div>
      {left.length === 0 ? <p className="muted">{events.length ? 'Nothing more today.' : 'Nothing on the calendar today.'}</p>
        : (
          <ul className="events compact">
            {left.map((e) => (
              <li key={e.id} className={`event ${e.busy}`}>
                <span className="event-time">{e.all_day ? 'All day' : clock(e.start)}</span>
                <span className="event-main">
                  <span className="event-title">{e.title}</span>
                </span>
                {e.join_url && <a className="button join" href={e.join_url} target="_blank" rel="noreferrer">Join</a>}
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}

export function CalendarSettings() {
  const status = usePolling<CalendarStatus>('/api/calendar')
  const [error, setError] = useState<string | null>(null)
  const [syncing, setSyncing] = useState(false)
  const { reload } = status

  const toggle = (id: number, enabled: boolean) =>
    patch(`/api/calendar/sources/${id}`, { enabled }).then(() => { setError(null); reload() }, (e) => setError(errorText(e)))
  const syncNow = () => {
    setSyncing(true)
    post('/api/calendar/sync').finally(() => setTimeout(() => { setSyncing(false); reload() }, 4000))
  }

  const data = status.data
  const accounts = new Map<string, CalendarStatus['sources']>()
  for (const s of data?.sources ?? []) accounts.set(s.account, [...(accounts.get(s.account) ?? []), s])

  return (
    <section>
      <h2>Calendar</h2>
      {data && (
        <p className="muted">
          {data.hub.configured
            ? <>Hub calendar on {data.hub.account}.{' '}
                {data.hub.upserts + data.hub.deletes > 0
                  ? `${data.hub.upserts + data.hub.deletes} changes waiting to be written.` : 'Up to date.'}</>
            : 'No hub calendar is set, so the merged calendar shows here in Tether only.'}
        </p>
      )}
      {data?.holds.map((h) => <HoldPanel key={h.source_id} hold={h} onDone={reload} />)}
      {[...accounts.entries()].map(([account, sources]) => (
        <div key={account} className="source-group">
          <div className="source-account">
            <span className="dot" style={{ background: sources[0].account_color }} />{account}
          </div>
          {sources.map((s) => (
            <label key={s.id} className="source">
              <input type="checkbox" checked={s.enabled} onChange={(e) => toggle(s.id, e.target.checked)} />
              <span className="grow">{s.name}</span>
              {s.enabled && <span className="muted">{s.events} events</span>}
            </label>
          ))}
        </div>
      ))}
      {data?.sources.length === 0 && <p className="muted">Calendars appear here a minute after an account connects.</p>}
      {error && <p className="error">{error}</p>}
      <button className="button secondary" onClick={syncNow} disabled={syncing}>
        {syncing ? 'Syncing…' : 'Sync calendars now'}
      </button>
    </section>
  )
}

function HoldPanel({ hold, onDone }: { hold: Hold; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const release = (action: 'apply' | 'discard') =>
    post(`/api/calendar/sources/${hold.source_id}/release`, { action }).then(onDone, (e) => setError(errorText(e)))
  return (
    <div className="hold">
      <p>
        <strong>{hold.events.length} events vanished from {hold.source} at once.</strong> Tether is holding their
        removal. Remove them from the hub calendar, or keep the copies there and stop managing them.
      </p>
      <ul>
        {hold.events.slice(0, 8).map((e, n) => (
          <li key={n}>{e.title} <span className="muted">{e.all_day ? e.start : new Date(e.start).toLocaleString()}</span></li>
        ))}
        {hold.events.length > 8 && <li className="muted">and {hold.events.length - 8} more</li>}
      </ul>
      <div className="editor-row">
        <button className="button" onClick={() => release('apply')}>Remove them</button>
        <button className="button secondary" onClick={() => release('discard')}>Keep the copies</button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  )
}
