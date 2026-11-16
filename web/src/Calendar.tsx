import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { patch, post, type AgendaEvent, type CalendarStatus, type Hold } from './api'
import { accountStyle, errorText, Linkified, SHORT_SCREEN, useMedia, usePolling, useSwipe } from './shared'
import { linkProps, navigate, type Location } from './route'

// ------------------------------------------------------------------ dates

/** Local calendar dates as YYYY-MM-DD; noon avoids daylight-saving edges. */
function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const noon = (iso: string) => new Date(`${iso}T12:00:00`)
function plusDays(iso: string, n: number): string {
  const d = noon(iso)
  d.setDate(d.getDate() + n)
  return isoDay(d)
}
const midnight = (iso: string) => new Date(`${iso}T00:00:00`)
const validDay = (s: string | null) => (s && /^\d{4}-\d{2}-\d{2}$/.test(s) && !isNaN(noon(s).getTime()) ? s : null)

/** The local day an event starts on. All-day events carry their date as-is. */
function dayOf(e: AgendaEvent): string {
  return e.all_day ? e.start : isoDay(new Date(e.start))
}

const clock = (iso: string | Date) => new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })

function timeRange(e: AgendaEvent): string {
  if (e.all_day) {
    const days = Math.round((noon(e.end).getTime() - noon(e.start).getTime()) / 864e5)
    return days > 1 ? `All day · ${days} days` : 'All day'
  }
  return e.end > e.start ? `${clock(e.start)} – ${clock(e.end)}` : clock(e.start)
}

function dayLabel(day: string, today: string): string {
  if (day === today) return 'Today'
  if (day === plusDays(today, 1)) return 'Tomorrow'
  return noon(day).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })
}

const BUSY_LABEL: Record<AgendaEvent['busy'], string | null> = {
  busy: null, tentative: 'Not answered', free: 'Free', oof: 'Out of office',
}

// ------------------------------------------------------------------ views

type View = 'day' | '3day' | 'workweek' | 'week' | 'schedule'
const VIEWS: { id: View; label: string; step: number }[] = [
  { id: 'day', label: 'Day', step: 1 },
  { id: '3day', label: '3 days', step: 3 },
  { id: 'workweek', label: 'Work week', step: 7 },
  { id: 'week', label: 'Week', step: 7 },
  { id: 'schedule', label: 'Schedule', step: 14 },
]
const VIEW_KEY = 'tether.calendar.view'

function savedView(): View {
  try {
    const v = localStorage.getItem(VIEW_KEY) as View | null
    if (v && VIEWS.some((x) => x.id === v)) return v
  } catch { /* private mode */ }
  return innerWidth < 700 ? '3day' : 'week'
}

/** The first and last day (inclusive) a view shows around an anchor date. Weeks start on Sunday. */
function range(view: View, anchor: string): [string, string] {
  const dow = noon(anchor).getDay()
  switch (view) {
    case 'day': return [anchor, anchor]
    case '3day': return [anchor, plusDays(anchor, 2)]
    case 'workweek': { const mon = plusDays(anchor, 1 - dow); return [mon, plusDays(mon, 4)] }   // the Mon–Fri of that Sunday week
    case 'week': { const sun = plusDays(anchor, -dow); return [sun, plusDays(sun, 6)] }
    case 'schedule': return [anchor, plusDays(anchor, 13)]
  }
}

function days(first: string, last: string): string[] {
  const out = []
  for (let d = first; d <= last; d = plusDays(d, 1)) out.push(d)
  return out
}

function shortTitle(view: View, first: string, last: string): string {
  const a = noon(first), b = noon(last)
  if (view === 'day') return a.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
  if (a.getMonth() === b.getMonth()) return a.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
  return `${a.toLocaleDateString(undefined, { month: 'short' })}–${b.toLocaleDateString(undefined, { month: 'short' })}`
}

function title(view: View, first: string, last: string): string {
  const a = noon(first), b = noon(last)
  if (view === 'day') return a.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
  if (a.getMonth() === b.getMonth() && a.getFullYear() === b.getFullYear())
    return a.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
  if (a.getFullYear() === b.getFullYear())
    return `${a.toLocaleDateString(undefined, { month: 'short' })} – ${b.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })}`
  return `${a.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })} – ${b.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })}`
}

export function CalendarPage({ loc, barSlot }: { loc: Location; barSlot: HTMLElement | null }) {
  const segment = loc.path.split('/')[2]
  const view = VIEWS.find((v) => v.id === segment)?.id
  const today = isoDay(new Date())
  const anchor = validDay(loc.params.get('date')) ?? today
  const [picked, setPicked] = useState<AgendaEvent | null>(null)
  const swipeTo = useRef<(dir: 1 | -1) => void>(() => {})
  const swiper = useSwipe(swipeTo)
  const short = useMedia(SHORT_SCREEN)

  useEffect(() => { if (!view) navigate(`/calendar/${savedView()}${location.search}`, { replace: true }) }, [view])
  useEffect(() => {
    if (view) try { localStorage.setItem(VIEW_KEY, view) } catch { /* private mode */ }
  }, [view])

  const [first, last] = range(view ?? 'week', anchor)
  // A day early, so an overnight event that started yesterday still shows this morning.
  const agenda = usePolling<AgendaEvent[]>(view ? `/api/agenda?start=${plusDays(first, -1)}&end=${last}` : '')
  if (!view) return null

  const go = (to: View, date: string) => navigate(`/calendar/${to}${date === today ? '' : `?date=${date}`}`)
  const step = VIEWS.find((v) => v.id === view)!.step
  swipeTo.current = (dir) => go(view, plusDays(anchor, dir * step))

  const toolbar = (
      <div className="cal-toolbar">
        <button className="button secondary small" onClick={() => go(view, today)}>Today</button>
        <button className="pager-button" aria-label="Previous" onClick={() => go(view, plusDays(anchor, -step))}>‹</button>
        <button className="pager-button" aria-label="Next" onClick={() => go(view, plusDays(anchor, step))}>›</button>
        <h1 className="cal-title">
          <span className="long">{title(view, first, last)}</span><span className="short">{shortTitle(view, first, last)}</span>
        </h1>
        <div className="segmented" role="tablist" aria-label="View">
          {VIEWS.map((v) => (
            <button key={v.id} role="tab" aria-selected={v.id === view} onClick={() => go(v.id, anchor)}>{v.label}</button>
          ))}
        </div>
        <select className="view-select" value={view} aria-label="View" onChange={(e) => go(e.target.value as View, anchor)}>
          {VIEWS.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
        </select>
      </div>
  )

  return (
    <main className={`calendar-page view-${view}${short ? ' short-screen' : ''}`}>
      {short && barSlot ? createPortal(toolbar, barSlot) : toolbar}
      {agenda.error && <p className="error">Couldn't load the calendar: {agenda.error}</p>}
      <div className="cal-swipe" ref={swiper}>
        {view === 'schedule'
          ? <Schedule events={(agenda.data ?? []).filter((e) => dayOf(e) >= first)} today={today} loaded={!!agenda.data} />
          : <TimeGrid days={days(first, last)} events={agenda.data ?? []} today={today} onPick={setPicked}
                      onDay={(d) => go('day', d)} />}
      </div>
      {picked && <EventDetail event={picked} onClose={() => setPicked(null)} />}
    </main>
  )
}

// ------------------------------------------------------------------ schedule

function Schedule({ events, today, loaded }: { events: AgendaEvent[]; today: string; loaded: boolean }) {
  const byDay = new Map<string, AgendaEvent[]>()
  for (const e of events) {
    const day = dayOf(e)
    byDay.set(day, [...(byDay.get(day) ?? []), e])
  }
  return (
    <>
      {loaded && events.length === 0 && (
        <p className="muted">Nothing in these two weeks. Choose which calendars to include in Settings.</p>
      )}
      {[...byDay.entries()].map(([day, list]) => (
        <section key={day} className="day">
          <h2 className={day === today ? 'today' : ''}>{dayLabel(day, today)}</h2>
          <ul className="events">
            {list.map((e) => <EventRow key={e.id} event={e} />)}
          </ul>
        </section>
      ))}
    </>
  )
}

function EventRow({ event: e }: { event: AgendaEvent }) {
  const [open, setOpen] = useState(false)
  const past = !e.all_day && new Date(e.end) < new Date()
  const detail = e.description || e.location
  return (
    <li className={`event tinted ${e.busy}${past ? ' past' : ''}`} style={eventStyle(e)}>
      <span className="event-time">{timeRange(e)}</span>
      <div className="event-main">
        <button className="event-title" onClick={() => setOpen(!open)} aria-expanded={open} disabled={!detail}>
          {e.title}
        </button>
        <span className="event-meta">
          <span className="muted">{e.sources.map((s) => s.name).join(', ')}</span>
          {BUSY_LABEL[e.busy] && <span className="busy-tag">{BUSY_LABEL[e.busy]}</span>}
          {e.location && <span className="muted location">{e.location}</span>}
        </span>
        {open && e.description && <p className="event-description"><Linkified text={e.description} /></p>}
      </div>
      {e.join_url && !past && (
        <a className="button join" href={e.join_url} target="_blank" rel="noreferrer">Join</a>
      )}
    </li>
  )
}

/** An event takes its first calendar's color; a second calendar shows as a stripe on the right. */
function eventStyle(e: AgendaEvent): React.CSSProperties {
  const style = accountStyle(e.sources[0]?.color) as Record<string, string>
  if (e.sources[1]) style['--acct2'] = e.sources[1].color
  return style as React.CSSProperties
}

// ------------------------------------------------------------------ time grid

const HOUR = 48                     // px per hour
const MIN_MINUTES = 20              // shortest an event is drawn

type Placed = { event: AgendaEvent; top: number; bottom: number; col: number; cols: number }

/** Timed events of one day, clipped to it and laid out side by side where they overlap. */
function layoutDay(day: string, events: AgendaEvent[]): Placed[] {
  const start = midnight(day).getTime(), end = midnight(plusDays(day, 1)).getTime()
  const segs = events
    .map((event) => {
      const s = Math.max(new Date(event.start).getTime(), start)
      const e = Math.min(Math.max(new Date(event.end).getTime(), new Date(event.start).getTime()), end)
      const top = (s - start) / 6e4
      return { event, top, bottom: Math.max(top + MIN_MINUTES, (e - start) / 6e4) }
    })
    .filter((p) => new Date(p.event.start).getTime() < end && (new Date(p.event.end).getTime() > start ||
                   new Date(p.event.start).getTime() >= start))
    .sort((a, b) => a.top - b.top || b.bottom - a.bottom)
  const placed: Placed[] = []
  let cluster: Placed[] = [], ends: number[] = [], clusterEnd = -1
  const close = () => { for (const p of cluster) p.cols = ends.length; cluster = []; ends = [] }
  for (const seg of segs) {
    if (seg.top >= clusterEnd) close()
    let col = ends.findIndex((e) => e <= seg.top)
    if (col === -1) { col = ends.length; ends.push(seg.bottom) } else ends[col] = seg.bottom
    const p = { ...seg, col, cols: 1 }
    cluster.push(p); placed.push(p)
    clusterEnd = Math.max(clusterEnd, seg.bottom)
  }
  close()
  return placed
}

/** All-day events, and timed ones a day or longer, go in the strip above the hours. */
const isBanner = (e: AgendaEvent) =>
  e.all_day || new Date(e.end).getTime() - new Date(e.start).getTime() >= 864e5

function bannerSpan(e: AgendaEvent, dayList: string[]): [number, number] | null {
  const startDay = e.all_day ? e.start : isoDay(new Date(e.start))
  const endDay = e.all_day ? e.end : plusDays(isoDay(new Date(new Date(e.end).getTime() - 1)), 1)   // exclusive
  const first = dayList[0], last = dayList[dayList.length - 1]
  if (endDay <= first || startDay > last) return null
  const a = Math.max(0, dayList.indexOf(startDay < first ? first : startDay))
  const b = endDay > last ? dayList.length : dayList.indexOf(endDay)
  return b > a ? [a, b] : null
}

type Banner = { e: AgendaEvent; span: [number, number] }

/** Rows for all-day events: each goes in the first row where it overlaps nothing, longest first. */
function bannerLanes(banners: Banner[]): (Banner & { lane: number })[] {
  const sorted = [...banners].sort((a, b) =>
    a.span[0] - b.span[0] || (b.span[1] - b.span[0]) - (a.span[1] - a.span[0]) || a.e.title.localeCompare(b.e.title))
  const ends: number[] = []   // per lane, the first free day
  return sorted.map((b) => {
    let lane = ends.findIndex((end) => end <= b.span[0])
    if (lane < 0) lane = ends.length
    ends[lane] = b.span[1]
    return { ...b, lane }
  })
}


/**
 * The all-day strip. Collapsed, it shows two rows (one on a short screen) and then a "+N more" for each day
 * with more than that; a tap on the strip (outside an event) or on the arrow opens every row.
 */
function AllDay({ banners, dayCount, expanded, onToggle, onPick }: {
  banners: Banner[]; dayCount: number; expanded: boolean; onToggle: () => void; onPick: (e: AgendaEvent) => void
}) {
  const placed = bannerLanes(banners)
  const collapsedLanes = useMedia(SHORT_SCREEN) ? 1 : 2
  const lanes = Math.max(...placed.map((b) => b.lane)) + 1
  // A third row is cheaper than a "+N more" row standing in for it.
  const collapsible = lanes > collapsedLanes + 1
  const shown = !collapsible || expanded ? lanes : collapsedLanes
  const hidden = Array.from({ length: dayCount }, (_, day) =>
    placed.filter((b) => b.lane >= shown && b.span[0] <= day && day < b.span[1]).length)
  return (
    <>
      <button className="cal-gutter all-day-label" onClick={collapsible ? onToggle : undefined}
              aria-expanded={collapsible ? expanded : undefined} disabled={!collapsible}>
        all day{collapsible && <span className="chevron">{expanded ? '▴' : '▾'}</span>}
      </button>
      <div className={`cal-banners${collapsible ? ' collapsible' : ''}${expanded ? ' expanded' : ''}`}
           style={{ gridColumn: `2 / span ${dayCount}`, gridTemplateColumns: `repeat(${dayCount}, minmax(0, 1fr))` }}
           onClick={(ev) => { if (collapsible && !(ev.target as HTMLElement).closest('.banner')) onToggle() }}>
        {placed.filter((b) => b.lane < shown).map(({ e, span, lane }) => (
          <button key={e.id} className={`banner tinted ${e.busy}`}
                  style={{ ...eventStyle(e), gridColumn: `${span[0] + 1} / ${span[1] + 1}`, gridRow: lane + 1 }}
                  onClick={() => onPick(e)} title={e.title}>
            {e.title}
          </button>
        ))}
        {shown < lanes && hidden.map((n, day) => n > 0 && (
          <span key={day} className="banner-more" style={{ gridColumn: day + 1, gridRow: shown + 1 }}>+{n} more</span>
        ))}
      </div>
    </>
  )
}

function TimeGrid({ days: dayList, events, today, onPick, onDay }: {
  days: string[]; events: AgendaEvent[]; today: string; onPick: (e: AgendaEvent) => void; onDay: (day: string) => void
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const [allDayOpen, setAllDayOpen] = useState(false)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(timer)
  }, [])

  // Open on the working day, or on the current hour when today is showing.
  const firstDay = dayList[0]
  const showsToday = dayList.includes(today)
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el) return
    const hour = showsToday ? Math.max(0, new Date().getHours() - 1.5) : 7.5
    el.scrollTop = hour * HOUR
  }, [firstDay, showsToday])

  const banners = events.filter(isBanner)
    .map((e) => ({ e, span: bannerSpan(e, dayList) }))
    .filter((b): b is Banner => b.span !== null)
  const timed = events.filter((e) => !isBanner(e))
  const cols = { gridTemplateColumns: `var(--gutter) repeat(${dayList.length}, minmax(0, 1fr))` }
  const nowMinutes = now.getHours() * 60 + now.getMinutes()

  return (
    <div className="cal-grid" ref={scroller}>
      <div className="cal-head" style={cols}>
        <div className="cal-gutter" />
        {dayList.map((d) => {
          const date = noon(d)
          return (
            <button key={d} className={`cal-day-head${d === today ? ' today' : ''}${d < today ? ' past' : ''}`}
                    onClick={() => onDay(d)} title="Open this day">
              <span className="dow">{date.toLocaleDateString(undefined, { weekday: 'short' })}</span>
              <span className="dom">{date.getDate()}</span>
            </button>
          )
        })}
        {banners.length > 0 && (
          <AllDay banners={banners} dayCount={dayList.length} expanded={allDayOpen}
                  onToggle={() => setAllDayOpen(!allDayOpen)} onPick={onPick} />
        )}
      </div>
      <div className="cal-body" style={{ ...cols, height: 24 * HOUR }}>
        <div className="cal-gutter hours">
          {Array.from({ length: 23 }, (_, n) => (
            <span key={n} style={{ top: (n + 1) * HOUR }}>
              {new Date(2000, 0, 1, n + 1).toLocaleTimeString(undefined, { hour: 'numeric' })}
            </span>
          ))}
        </div>
        {dayList.map((d) => (
          <div key={d} className={`cal-col${d === today ? ' today' : ''}`}>
            {layoutDay(d, timed).map((p) => {
              const height = (p.bottom - p.top) * HOUR / 60 - 2
              const past = new Date(p.event.end) < now
              return (
                <button key={p.event.id}
                        className={`slot tinted ${p.event.busy}${past ? ' past' : ''}${height < 34 ? ' short' : ''}`}
                        style={{ ...eventStyle(p.event), top: p.top * HOUR / 60, height,
                                 left: `${(p.col / p.cols) * 100}%`, width: `calc(${100 / p.cols}% - 2px)` }}
                        onClick={() => onPick(p.event)}
                        title={`${p.event.title}\n${timeRange(p.event)}`}>
                  <span className="slot-title">{p.event.title}</span>
                  <span className="slot-time">{clock(p.event.start)}{height >= 34 && ` – ${clock(p.event.end)}`}</span>
                  {height >= 64 && p.event.location && <span className="slot-where">{p.event.location}</span>}
                </button>
              )
            })}
            {d === today && <div className="now-line" style={{ top: nowMinutes * HOUR / 60 }} />}
          </div>
        ))}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ detail

function EventDetail({ event: e, onClose }: { event: AgendaEvent; onClose: () => void }) {
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => ev.key === 'Escape' && onClose()
    addEventListener('keydown', onKey)
    return () => removeEventListener('keydown', onKey)
  }, [onClose])
  const past = !e.all_day && new Date(e.end) < new Date()
  const when = e.all_day
    ? `${noon(e.start).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })} · ${timeRange(e)}`
    : `${new Date(e.start).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })} · ${timeRange(e)}`
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet tinted" style={eventStyle(e)} role="dialog" aria-modal="true" aria-label={e.title}
           onClick={(ev) => ev.stopPropagation()}>
        <div className="sheet-head">
          <h2 className="sheet-title">{e.title}</h2>
          <button className="sheet-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <p className="sheet-when">{when}</p>
        <div className="event-meta">
          {e.sources.map((s) => (
            <span key={s.source_id} className="source-tag" style={accountStyle(s.color)}>{s.name}</span>
          ))}
          {BUSY_LABEL[e.busy] && <span className="busy-tag">{BUSY_LABEL[e.busy]}</span>}
        </div>
        {e.location && <p className="sheet-where"><Linkified text={e.location} /></p>}
        {e.join_url && !past && (
          <a className="button join" href={e.join_url} target="_blank" rel="noreferrer">Join meeting</a>
        )}
        {e.description && <div className="sheet-description"><Linkified text={e.description} /></div>}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ home card

/** Today's events on the home screen: what's left of the day, then a link to the full calendar. */
export function TodayCard({ events }: { events: AgendaEvent[] }) {
  const now = new Date()
  const left = events.filter((e) => e.all_day || new Date(e.end) > now)
  return (
    <section className="today-card">
      <div className="card-head">
        <h2>Today</h2>
        <a className="link" {...linkProps('/calendar')}>Calendar</a>
      </div>
      {left.length === 0 ? <p className="muted">{events.length ? 'Nothing more today.' : 'Nothing on the calendar today.'}</p>
        : (
          <ul className="events compact">
            {left.map((e) => (
              <li key={e.id} className={`event tinted ${e.busy}`} style={eventStyle(e)}>
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

// ------------------------------------------------------------------ settings

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
