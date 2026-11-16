import { useEffect, useRef, useState } from 'react'
import { get, post, type AgendaEvent, type CalendarSource, type CalendarStatus } from './api'
import { addDays, errorText } from './shared'

/**
 * Creating an event: a double-click (or a long press) on an empty spot of the calendar opens this
 * editor with a 30-minute draft there. The draft is drawn on the grid as it is edited, and saved
 * to the chosen account's Google calendar.
 */

type Repeat = '' | 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'yearly'

export type Draft = {
  title: string
  date: string        // the first day
  endDate: string     // the last day (inclusive), for all-day events
  start: number       // minutes after midnight on date
  end: number         // minutes after midnight on date; past 1440 ends the next day
  allDay: boolean
  sourceId: number | null
  color: string | null
  guests: string[]
  notify: boolean
  meet: boolean
  location: string
  description: string
  busy: 'busy' | 'free'
  repeat: Repeat
}

export const DRAFT_ID = -1
const DEFAULT_MINUTES = 30
const LAST_SOURCE_KEY = 'tether.calendar.newIn'
const EMAIL = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/

export function newDraft(date: string, minutes: number | null): Draft {
  const start = minutes ?? 9 * 60
  return {
    title: '', date, endDate: date, start, end: start + DEFAULT_MINUTES, allDay: minutes === null,
    sourceId: null, color: null, guests: [], notify: true, meet: false, location: '', description: '',
    busy: 'busy', repeat: '',
  }
}

const pad = (n: number) => String(n).padStart(2, '0')
const hhmm = (m: number) => `${pad(Math.floor(m / 60) % 24)}:${pad(m % 60)}`
/** Local wall-clock time, minutes after midnight on date, as YYYY-MM-DDTHH:MM. */
const wallClock = (date: string, minutes: number) => `${addDays(date, Math.floor(minutes / 1440))}T${hhmm(minutes % 1440)}`

/** The draft as an event, so the grid draws it where it will land. */
export function draftEvent(d: Draft): AgendaEvent {
  const common = {
    id: DRAFT_ID, title: d.title.trim() || '(No title)', location: null, busy: d.busy, join_url: null,
    sources: [{ source_id: d.sourceId ?? 0, name: '', color: d.color ?? 'var(--accent)' }],
  }
  return d.allDay
    ? { ...common, all_day: true, start: d.date, end: addDays(d.endDate, 1) }
    : { ...common, all_day: false, start: `${wallClock(d.date, d.start)}:00`, end: `${wallClock(d.date, d.end)}:00` }
}

const timeLabel = (m: number) =>
  new Date(2000, 0, 1, Math.floor(m / 60) % 24, m % 60).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })

function duration(m: number): string {
  if (m < 60) return `${m} mins`
  const h = Math.round((m / 60) * 100) / 100
  return `${h} hr${h === 1 ? '' : 's'}`
}

function repeatOptions(date: string): [Repeat, string][] {
  const d = new Date(`${date}T12:00:00`)
  const weekday = d.toLocaleDateString(undefined, { weekday: 'long' })
  return [
    ['', 'Does not repeat'],
    ['daily', 'Daily'],
    ['weekly', `Weekly on ${weekday}`],
    ['monthly', `Monthly on day ${d.getDate()}`],
    ['yearly', `Annually on ${d.toLocaleDateString(undefined, { month: 'long', day: 'numeric' })}`],
    ['weekdays', 'Every weekday (Monday to Friday)'],
  ]
}

const daysBetween = (a: string, b: string) =>
  Math.round((new Date(`${b}T12:00:00`).getTime() - new Date(`${a}T12:00:00`).getTime()) / 864e5)

export type Saved = { id: string; link: string | null; shown: boolean; source: string }

export function EventEditor({ draft, update, onClose, onSaved }: {
  draft: Draft
  update: (changes: Partial<Draft>) => void
  onClose: () => void
  onSaved: (saved: Saved) => void
}) {
  const [calendars, setCalendars] = useState<CalendarSource[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [guestText, setGuestText] = useState('')
  const titleRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let live = true
    get<CalendarStatus>('/api/calendar').then((s) => {
      if (!live) return
      const writable = s.sources.filter((c) => c.writable)
      setCalendars(writable)
      if (draft.sourceId === null && writable.length) {
        let last: number | null = null
        try { last = Number(localStorage.getItem(LAST_SOURCE_KEY)) || null } catch { /* private mode */ }
        const pick = writable.find((c) => c.id === last)
          ?? writable.find((c) => c.is_primary && c.enabled) ?? writable.find((c) => c.enabled) ?? writable[0]
        update({ sourceId: pick.id, color: pick.account_color })
      }
    }, (e) => live && setError(errorText(e)))
    return () => { live = false }
  }, [])

  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    titleRef.current?.focus()
    const onKey = (ev: KeyboardEvent) => ev.key === 'Escape' && close.current()
    addEventListener('keydown', onKey)
    return () => removeEventListener('keydown', onKey)
  }, [])

  const source = calendars?.find((c) => c.id === draft.sourceId)

  const addGuests = (text: string): string[] | null => {
    const found = text.split(/[\s,;]+/).map((g) => g.replace(/^<|>$/g, '').trim().toLowerCase()).filter(Boolean)
    const bad = found.find((g) => !EMAIL.test(g))
    if (bad) { setError(`Not an email address: ${bad}`); return null }
    const guests = [...new Set([...draft.guests, ...found])]
    update({ guests })
    setGuestText('')
    setError(null)
    return guests
  }

  const save = async () => {
    if (saving) return
    if (!draft.sourceId) { setError('Choose a calendar.'); return }
    const guests = guestText.trim() ? addGuests(guestText) : draft.guests
    if (!guests) return
    setSaving(true)
    setError(null)
    try {
      const saved = await post<Omit<Saved, 'source'>>('/api/calendar/events', {
        source_id: draft.sourceId,
        title: draft.title,
        all_day: draft.allDay,
        start: draft.allDay ? draft.date : wallClock(draft.date, draft.start),
        end: draft.allDay ? draft.endDate : wallClock(draft.date, draft.end),
        time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        location: draft.location,
        description: draft.description,
        guests,
        notify: draft.notify,
        meet: draft.meet,
        busy: draft.busy,
        repeat: draft.repeat || null,
      })
      try { localStorage.setItem(LAST_SOURCE_KEY, String(draft.sourceId)) } catch { /* private mode */ }
      onSaved({ ...saved, source: source ? calendarName(source) : 'the calendar' })
    } catch (e) {
      setError(errorText(e))
      setSaving(false)
    }
  }

  const setStart = (start: number) => update({ start, end: start + (draft.end - draft.start) })
  const setDate = (date: string) => {
    if (!date) return
    update({ date, endDate: addDays(date, Math.max(0, daysBetween(draft.date, draft.endDate))) })
  }
  const starts = Array.from({ length: 96 }, (_, n) => n * 15)
  if (!starts.includes(draft.start)) starts.push(draft.start)
  const ends = Array.from({ length: 96 }, (_, n) => draft.start + (n + 1) * 15)
  if (!ends.includes(draft.end)) ends.push(draft.end)
  ends.sort((a, b) => a - b)

  return (
    <div className="ne-backdrop" onClick={onClose}>
      <form className="ne" role="dialog" noValidate aria-modal="true" aria-label="New event"
            onClick={(ev) => ev.stopPropagation()}
            onSubmit={(ev) => { ev.preventDefault(); save() }}
            onKeyDown={(ev) => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); save() } }}>
        <div className="ne-top">
          <button type="button" className="sheet-close" onClick={onClose} aria-label="Discard">×</button>
          <span className="ne-heading">New event</span>
          <button type="submit" className="button small" disabled={saving || !calendars?.length}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>

        <input ref={titleRef} className="ne-title" placeholder="Add title" aria-label="Title" value={draft.title}
               onChange={(e) => update({ title: e.target.value })} maxLength={1024} />

        <div className="ne-row">
          <Icon d={ICONS.clock} />
          <div className={`ne-when${draft.allDay ? ' all-day' : ''}`}>
            <input type="date" aria-label="Date" value={draft.date} onChange={(e) => setDate(e.target.value)} />
            {draft.allDay ? (
              <>
                <span className="muted">–</span>
                <input type="date" aria-label="End date" value={draft.endDate} min={draft.date}
                       onChange={(e) => e.target.value && update({ endDate: e.target.value })} />
              </>
            ) : (
              <>
                <select aria-label="Start time" value={draft.start} onChange={(e) => setStart(Number(e.target.value))}>
                  {starts.map((m) => <option key={m} value={m}>{timeLabel(m)}</option>)}
                </select>
                <span className="muted">–</span>
                <select aria-label="End time" value={draft.end} onChange={(e) => update({ end: Number(e.target.value) })}>
                  {ends.map((m) => (
                    <option key={m} value={m}>
                      {timeLabel(m)}{m >= 1440 ? ' next day' : ''} ({duration(m - draft.start)})
                    </option>
                  ))}
                </select>
              </>
            )}
          </div>
        </div>
        <div className="ne-row">
          <span />
          <div className="ne-fields">
            <label className="ne-check">
              <input type="checkbox" checked={draft.allDay}
                     onChange={(e) => update({ allDay: e.target.checked, endDate: draft.date })} />
              All day
            </label>
            <select className="ne-repeat" aria-label="Repeat" value={draft.repeat} onChange={(e) => update({ repeat: e.target.value as Repeat })}>
              {repeatOptions(draft.date).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </div>
        </div>

        <div className="ne-row">
          <Icon d={ICONS.people} />
          <div className="ne-stack">
            {draft.guests.length > 0 && (
              <ul className="ne-chips" aria-label="Guests">
                {draft.guests.map((g) => (
                  <li key={g}>
                    <span>{g}</span>
                    <button type="button" aria-label={`Remove ${g}`}
                            onClick={() => update({ guests: draft.guests.filter((x) => x !== g) })}>×</button>
                  </li>
                ))}
              </ul>
            )}
            <input type="email" className="ne-wide" placeholder="Add guests" aria-label="Add guests" value={guestText}
                   autoComplete="email" multiple
                   onChange={(e) => setGuestText(e.target.value)}
                   onBlur={() => guestText.trim() && addGuests(guestText)}
                   onKeyDown={(e) => {
                     if ((e.key === 'Enter' || e.key === ',' || e.key === ';') && guestText.trim()) {
                       e.preventDefault()
                       addGuests(guestText)
                     } else if (e.key === 'Backspace' && !guestText && draft.guests.length) {
                       update({ guests: draft.guests.slice(0, -1) })
                     }
                   }} />
            {draft.guests.length > 0 && (
              <label className="ne-check">
                <input type="checkbox" checked={draft.notify} onChange={(e) => update({ notify: e.target.checked })} />
                Email invitations to guests
              </label>
            )}
          </div>
        </div>

        <div className="ne-row">
          <Icon d={ICONS.video} />
          {draft.meet ? (
            <div className="ne-fields">
              <span className="ne-meet-on">Google Meet video conferencing is added when you save</span>
              <button type="button" className="link" onClick={() => update({ meet: false })}>Remove</button>
            </div>
          ) : (
            <div className="ne-fields">
              <button type="button" className="button secondary small" onClick={() => update({ meet: true })}>
                Add Google Meet video conferencing
              </button>
            </div>
          )}
        </div>

        <div className="ne-row">
          <Icon d={ICONS.pin} />
          <input className="ne-wide" placeholder="Add location" aria-label="Location" value={draft.location}
                 onChange={(e) => update({ location: e.target.value })} maxLength={1024} />
        </div>

        <div className="ne-row">
          <Icon d={ICONS.notes} />
          <textarea className="ne-wide" placeholder="Add description" aria-label="Description" rows={3}
                    value={draft.description} onChange={(e) => update({ description: e.target.value })} maxLength={8000} />
        </div>

        <div className="ne-row">
          <Icon d={ICONS.calendar} />
          <div className="ne-fields">
            <span className="ne-dot" style={{ background: source?.account_color ?? 'var(--muted)' }} />
            {calendars === null ? <span className="muted">Loading calendars…</span>
              : calendars.length === 0 ? <span className="muted">No calendar here accepts new events. Connect a Google account in Settings.</span>
              : (
                <select className="ne-calendar" aria-label="Calendar" value={draft.sourceId ?? ''}
                        onChange={(e) => {
                          const c = calendars.find((x) => x.id === Number(e.target.value))
                          if (c) update({ sourceId: c.id, color: c.account_color })
                        }}>
                  {calendars.map((c) => (
                    <option key={c.id} value={c.id}>{calendarName(c)}{c.enabled ? '' : ' (not shown in Tether)'}</option>
                  ))}
                </select>
              )}
            <select aria-label="Show as" value={draft.busy} onChange={(e) => update({ busy: e.target.value as Draft['busy'] })}>
              <option value="busy">Busy</option>
              <option value="free">Free</option>
            </select>
          </div>
        </div>

        {error && <p className="error ne-error">{error}</p>}
      </form>
    </div>
  )
}

/** A calendar as named in the picker: an account's own calendar goes by the account, others say whose they are. */
function calendarName(c: CalendarSource): string {
  return c.is_primary || c.name === c.account ? c.account : `${c.name} · ${c.account}`
}

const ICONS = {
  clock: 'M12 7v5l3 2 M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18z',
  people: 'M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1 M9 11a3.5 3.5 0 1 0 0-7a3.5 3.5 0 0 0 0 7z M22 19v-1a4 4 0 0 0-3-3.9 M16 4.1a3.5 3.5 0 0 1 0 6.8',
  video: 'M15 10l5-3v10l-5-3z M3 7h12v10H3z',
  pin: 'M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z M12 12a2.5 2.5 0 1 0 0-5a2.5 2.5 0 0 0 0 5z',
  notes: 'M4 6h16 M4 10h16 M4 14h16 M4 18h10',
  calendar: 'M4 6h16v14H4z M4 10h16 M8 3v5 M16 3v5',
}

const Icon = ({ d }: { d: string }) => (
  <svg className="ne-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none"
       stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d={d} />
  </svg>
)
