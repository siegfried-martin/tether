import { useEffect, useState } from 'react'
import { get, type Item, type Thread, type ThreadMessage } from './api'
import { InviteCard } from './Invite'
import { DueLabel, errorText, Linkified } from './shared'

export const ExternalIcon = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 4h6v6" /><path d="M20 4l-9 9" /><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
  </svg>
)

/** What a link opens, for its label. */
export function externalName(url: string | null): string {
  if (!url) return ''
  if (url.includes('mail.google.com')) return 'Gmail'
  if (/outlook|office|live\.com/.test(url)) return 'Outlook'
  return 'browser'
}

/**
 * The item read inside Tether. Gmail threads are fetched live and shown as text;
 * a digest item shows what the digest said about it.
 */
export function ReadingPane({ itemId, kind, url, digest }: {
  itemId: number; kind: Item['kind']; url: string | null; digest?: Item
}) {
  return (
    <div className="reading">
      {kind === 'email' ? <ThreadView itemId={itemId} /> : digest ? <DigestView item={digest} /> : null}
      {url && (
        <a className="reading-out" href={url} target="_blank" rel="noreferrer">
          Open in {externalName(url)} <ExternalIcon />
        </a>
      )}
    </div>
  )
}

function ThreadView({ itemId }: { itemId: number }) {
  const [thread, setThread] = useState<Thread | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    get<Thread>(`/api/items/${itemId}/thread`).then((t) => live && setThread(t), (e) => live && setError(errorText(e)))
    return () => { live = false }
  }, [itemId])
  if (error) return <p className="error">Couldn't load this email: {error}</p>
  if (!thread) return <p className="muted">Loading…</p>
  return (
    <div className="thread">
      {thread.invite && <InviteCard itemId={itemId} />}
      {thread.messages.length > 1 && <h3 className="thread-subject">{thread.subject} <span className="muted">· {thread.messages.length} messages</span></h3>}
      {thread.messages.map((m, n) => <Message key={n} message={m} startOpen={n === thread.messages.length - 1} />)}
    </div>
  )
}

function sender(from: string): string {
  const match = from.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>\s*$/)
  return match ? match[1] : from
}

function Message({ message: m, startOpen }: { message: ThreadMessage; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen)
  const when = new Date(m.date).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
  return (
    <article className={`message${open ? ' open' : ''}`}>
      <button className="message-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="message-from">{m.sent_by_me ? 'Me' : sender(m.from)}</span>
        {!open && <span className="message-peek">{m.body.slice(0, 160)}</span>}
        <time dateTime={m.date}>{when}</time>
      </button>
      {open && (
        <>
          <div className="message-to muted">
            to {m.to || '—'}{m.cc && <> · cc {m.cc}</>}
          </div>
          <div className="message-body"><Linkified text={m.body || '(no text)'} /></div>
          {m.truncated && <p className="muted">This message is long; open it externally to read the rest.</p>}
          {m.attachments.length > 0 && (
            <ul className="attachments">
              {m.attachments.map((a, n) => <li key={n}>📎 {a.name} <span className="muted">{Math.ceil(a.size / 1024)} KB</span></li>)}
            </ul>
          )}
        </>
      )}
    </article>
  )
}

function DigestView({ item }: { item: Item }) {
  return (
    <dl className="digest-view">
      <dt>From</dt><dd>{item.sender || '—'}</dd>
      <dt>Subject</dt><dd>{item.title}</dd>
      {item.needed && <><dt>Needed</dt><dd>{item.needed}</dd></>}
      {(item.due_at || item.priority === 'high') && (
        <><dt>Due</dt><dd>{item.due_at ? <DueLabel due={item.due_at} /> : 'No date'}{item.priority === 'high' && <span className="flag"> High</span>}</dd></>
      )}
      <dt>Received</dt><dd>{new Date(item.last_activity_at).toLocaleString()}</dd>
    </dl>
  )
}
