import { useEffect, useState } from 'react'
import { get, post, type AgendaEvent, type Invite, type InviteAnswer } from './api'
import { clock, eventStyle, HOUR, isoDay, layoutDay, plusDays } from './Calendar'
import { errorText, localToday } from './shared'

const ANSWERS: { id: InviteAnswer; label: string; done: string }[] = [
  { id: 'accepted', label: 'Yes', done: 'Going' },
  { id: 'tentative', label: 'Maybe', done: 'Maybe' },
  { id: 'declined', label: 'No', done: 'Not going' },
]
const PREVIEW_ID = -1
const MIN_HOURS = 4

const noon = (day: string) => new Date(`${day}T12:00:00`)
const sameTitle = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase()

function whenLabel(inv: Invite): string {
  const today = localToday()
  const day = inv.all_day ? inv.start : isoDay(new Date(inv.start))
  const name = day === today ? 'Today' : day === plusDays(today, 1) ? 'Tomorrow'
    : noon(day).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric',
                                               year: day.slice(0, 4) === today.slice(0, 4) ? undefined : 'numeric' })
  if (inv.all_day) {
    const days = Math.round((noon(inv.end).getTime() - noon(inv.start).getTime()) / 864e5)
    return days > 1 ? `${name} · ${days} days` : `${name} · All day`
  }
  return `${name} · ${clock(inv.start)} – ${clock(inv.end)}`
}

function statusLine(inv: Invite): string | null {
  if (inv.cancelled) return 'This event was cancelled.'
  if (inv.reply) {
    const said = { accepted: 'accepted', tentative: 'said maybe', declined: 'declined' }[inv.reply.response ?? '']
    return `${inv.reply.who} ${said ?? 'replied'}.`
  }
  if (!inv.on_calendar) {
    return inv.method === 'REQUEST' ? `Not on ${inv.account}'s calendar. Open the email in Gmail to answer.`
      : `Not on ${inv.account}'s calendar.`   // PUBLISH: a booking or ticket's "add to calendar" file
  }
  if (!inv.can_respond) return null
  return inv.response === 'needsAction' || !inv.response ? "You haven't answered yet." : null
}

/**
 * A calendar invitation in an email: what and when, a slice of that day's calendar around it
 * (with clashes called out), and Yes / Maybe / No, answered as the invited account.
 */
export function InviteCard({ itemId }: { itemId: number }) {
  const [invite, setInvite] = useState<Invite | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState<InviteAnswer | null>(null)
  const [sent, setSent] = useState(false)
  useEffect(() => {
    let live = true
    get<{ invite: Invite | null }>(`/api/items/${itemId}/invite`)
      .then((r) => live && setInvite(r.invite), (e) => live && setError(errorText(e)))
    return () => { live = false }
  }, [itemId])

  if (error) return <p className="error">Couldn't read the invitation: {error}</p>
  if (!invite) return null

  const answer = async (response: InviteAnswer) => {
    setSending(response); setError(null)
    try {
      const r = await post<{ invite: Invite }>(`/api/items/${itemId}/invite/respond`, { response })
      setInvite(r.invite); setSent(true)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setSending(null)
    }
  }
  const status = statusLine(invite)
  const meta = [invite.organizer && `Organizer: ${invite.organizer}`,
                invite.guests > 1 && `${invite.guests} guests`, invite.recurring && 'Repeats'].filter(Boolean)
  return (
    <section className={`invite${invite.cancelled ? ' cancelled' : ''}`} aria-label="Calendar invitation">
      <div className="invite-head">
        <CalendarIcon />
        <div className="invite-what">
          <h3 className="invite-title">{invite.title}</h3>
          <div className="invite-when">{whenLabel(invite)}</div>
          {invite.location && <div className="invite-where muted">{invite.location}</div>}
          {meta.length > 0 && <div className="invite-meta muted">{meta.join(' · ')}</div>}
        </div>
      </div>
      {!invite.cancelled && <DayPreview invite={invite} />}
      {status && <p className="invite-status muted">{status}</p>}
      {invite.can_respond && (
        <div className="invite-answer">
          <span className="invite-ask">Going?</span>
          <div className="invite-buttons" role="group" aria-label="Answer the invitation">
            {ANSWERS.map((a) => (
              <button key={a.id} className={`invite-button ${a.id}${invite.response === a.id ? ' chosen' : ''}`}
                      aria-pressed={invite.response === a.id} disabled={sending !== null}
                      onClick={() => invite.response !== a.id && answer(a.id)}>
                {sending === a.id ? '…' : invite.response === a.id ? `✓ ${a.done}` : a.label}
              </button>
            ))}
          </div>
        </div>
      )}
      {sent && invite.organizer && <p className="invite-status muted">Sent. {invite.organizer} will get your answer.</p>}
      {invite.calendar_url && (
        <a className="invite-link" href={invite.calendar_url} target="_blank" rel="noreferrer">Open in Google Calendar</a>
      )}
    </section>
  )
}

/** Hours around the invite on its day, with the rest of the calendar drawn beside it. */
function DayPreview({ invite }: { invite: Invite }) {
  const day = invite.all_day ? invite.start : isoDay(new Date(invite.start))
  const [events, setEvents] = useState<AgendaEvent[] | null>(null)
  useEffect(() => {
    let live = true
    // The agenda's days are the server's; a day either side covers any zone difference.
    get<AgendaEvent[]>(`/api/agenda?start=${plusDays(day, -1)}&end=${plusDays(day, 1)}`)
      .then((r) => live && setEvents(r), () => live && setEvents([]))
    return () => { live = false }
  }, [day])
  if (!events) return <div className="invite-day loading" />

  // The invite's own copy on the calendar (shown until declined) is drawn as the invite instead.
  const others = events.filter((e) => !(sameTitle(e.title, invite.title) &&
    (invite.all_day ? e.start === invite.start : new Date(e.start).getTime() === new Date(invite.start).getTime())))

  if (invite.all_day) {
    const that = others.filter((e) => (e.all_day ? e.start <= day && e.end > day : isoDay(new Date(e.start)) === day))
    return (
      <div className="invite-day list">
        {that.length === 0 ? <span className="muted">Nothing else that day.</span> : (
          <ul>
            {that.map((e) => (
              <li key={e.id} className="tinted" style={eventStyle(e)}>
                <span className="invite-list-time">{e.all_day ? 'All day' : clock(e.start)}</span> {e.title}
              </li>
            ))}
          </ul>
        )}
      </div>
    )
  }

  const self: AgendaEvent = { id: PREVIEW_ID, title: invite.title, start: invite.start, end: invite.end,
                              all_day: false, location: invite.location, busy: 'busy', join_url: null, sources: [] }
  const timed = others.filter((e) => !e.all_day)
  const placed = layoutDay(day, [...timed, self])
  const mine = placed.find((p) => p.event.id === PREVIEW_ID)
  if (!mine) return null
  const first = Math.max(0, Math.floor(mine.top / 60) - 1)
  const last = Math.min(24, Math.max(first + MIN_HOURS, Math.ceil(mine.bottom / 60) + 1))
  const visible = placed.filter((p) => p.bottom > first * 60 && p.top < last * 60)
  const clashes = visible.filter((p) => p.event.id !== PREVIEW_ID && p.event.busy !== 'free' &&
                                        p.top < mine.bottom && p.bottom > mine.top)
  const allDay = others.filter((e) => e.all_day && e.start <= day && e.end > day)
  const px = (minutes: number) => (minutes - first * 60) * HOUR / 60
  return (
    <>
      {allDay.length > 0 && (
        <div className="invite-allday">
          {allDay.map((e) => <span key={e.id} className="tinted" style={eventStyle(e)}>{e.title}</span>)}
        </div>
      )}
      <div className="invite-day" style={{ height: (last - first) * HOUR }}>
        <div className="hours">
          {Array.from({ length: last - first - 1 }, (_, n) => (
            <span key={n} style={{ top: (n + 1) * HOUR }}>
              {new Date(2000, 0, 1, first + n + 1).toLocaleTimeString(undefined, { hour: 'numeric' })}
            </span>
          ))}
        </div>
        <div className="cal-col">
          {visible.map((p) => {
            const top = Math.max(0, px(p.top)), bottom = Math.min((last - first) * HOUR, px(p.bottom))
            const height = bottom - top - 2
            const isInvite = p.event.id === PREVIEW_ID
            return (
              <div key={p.event.id}
                   className={`slot ${isInvite ? 'invite-slot' : `tinted ${p.event.busy}`}${height < 34 ? ' short' : ''}`}
                   style={{ ...(isInvite ? {} : eventStyle(p.event)), top, height,
                            left: `${(p.col / p.cols) * 100}%`, width: `calc(${100 / p.cols}% - 2px)` }}>
                <span className="slot-title">{p.event.title}</span>
                <span className="slot-time">{clock(p.event.start)}{height >= 34 && ` – ${clock(p.event.end)}`}</span>
              </div>
            )
          })}
        </div>
      </div>
      {clashes.length > 0 && (
        <p className="invite-clash">
          Clashes with {clashes.map((p) => p.event.title).slice(0, 3).join(', ')}
          {clashes.length > 3 && ` and ${clashes.length - 3} more`}
        </p>
      )}
    </>
  )
}

const CalendarIcon = () => (
  <svg className="invite-icon" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="5" width="18" height="16" rx="2" /><path d="M16 3v4M8 3v4M3 10h18" />
  </svg>
)
