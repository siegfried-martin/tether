export type Item = {
  id: number
  kind: 'email' | 'chat' | 'digest'
  title: string
  sender: string
  snippet: string
  needed: string | null
  url: string | null
  last_activity_at: string
  last_direction: 'in' | 'out'
  state: 'open' | 'archived'
  archived_at: string | null
  priority: string | null
  due_at: string | null
  account_id: number
  account: string
  account_color: string
  open_tasks: number
  note?: string
}

export type Task = {
  id: number
  parent_id: number | null
  title: string
  notes: string | null
  status: 'open' | 'done'
  due_at: string | null
  priority: 'high' | 'normal' | 'low' | null
  sort_order: number
  item_id: number | null
  completed_at: string | null
  item_title: string | null
  item_url: string | null
  item_state: 'open' | 'archived' | null
  item_kind: Item['kind'] | null
  item_account: string | null
  item_color: string | null
  tags: TagRef[]
}

export type TagRef = { id: number; name: string; color: string }
export type Tag = TagRef & { open_tasks: number; done_tasks: number }

export type ItemPage = { items: Item[]; total: number }
export type TaskPage = { tasks: Task[]; total: number; open: number; offset: number; focus: number | null; found: boolean }

/** A device or client signed in to Tether: the Android app, or an MCP connector such as Claude. */
export type Connection = { id: number; kind: 'app' | 'mcp'; name: string; created_at: string; last_active: string }

export type ThreadMessage = {
  from: string
  to: string
  cc: string
  date: string
  sent_by_me: boolean
  body: string
  truncated: boolean
  attachments: { name: string; size: number; type: string }[]
}

export type Thread = {
  account: string; subject: string; item_id: number | null; invite?: boolean; messages: ThreadMessage[]
}

export type InviteAnswer = 'accepted' | 'tentative' | 'declined'

/** A calendar invitation found in an email thread (GET /api/items/{id}/invite). */
export type Invite = {
  method: string
  title: string
  start: string
  end: string
  all_day: boolean
  location: string | null
  organizer: string | null
  organizer_email: string | null
  recurring: boolean
  guests: number
  cancelled: boolean
  reply: { who: string; response: string | null } | null
  account: string
  on_calendar: boolean
  response: InviteAnswer | 'needsAction' | null
  can_respond: boolean
  calendar_url: string | null
}

export type Alert = { kind: string; account: string; message: string }

export type Overview = {
  today: string
  urgent: { alerts: Alert[]; tasks: Task[]; follow_ups: unknown[]; items: Item[] }
  inbox: { count: number; by_account: Record<string, number>; preview: Item[] }
  tasks: { count: number }
  calendar: { today: AgendaEvent[]; holds: Hold[] }
}

export type AgendaEvent = {
  id: number
  title: string
  start: string
  end: string
  all_day: boolean
  location: string | null
  busy: 'busy' | 'tentative' | 'free' | 'oof'
  join_url: string | null
  description?: string | null
  sources: EventSource[]
}

export type EventSource = {
  source_id: number
  name: string
  color: string
  // Present on the web app's agenda: whether Tether can delete the event from this calendar.
  deletable?: boolean
  recurring?: boolean   // one occurrence of a repeating series
  organizer?: boolean   // the calendar's owner organizes it (not just a guest)
  guests?: number       // other people invited
}

export type DeleteResult = {
  deleted: number
  series: boolean
  sources: string[]
  remaining: string[]   // calendars Tether can't change, which keep their copy
  undo: { source_id: number; event_id: string }[] | null
}

export type Hold = { source_id: number; source: string; events: { title: string; start: string; all_day: boolean }[] }

export type CalendarSource = {
  id: number
  kind: 'google' | 'ics'
  name: string
  enabled: boolean
  account_id: number
  account: string
  account_color: string
  events: number
  held: number
  writable: boolean     // takes new events from Tether
  is_primary: boolean   // the account's own main calendar
}

export type CalendarStatus = {
  hub: { configured: boolean; account: string | null; upserts: number; deletes: number }
  sources: CalendarSource[]
  holds: Hold[]
}

export type SyncSettings = {
  excluded_categories: string[]
  initial_sync_days: number
  categories: string[]
  max_days: number
}

export type Account = { id: number; kind: string; label: string; color: string; status: string; sync?: SyncSettings }

export type SyncRow = {
  account: string
  kind: string
  status: string
  stream: string | null
  last_attempt_at: string | null
  last_ok_at: string | null
  failures: number | null
  last_error: string | null
  source: string | null
}

export class Unauthorized extends Error {}

export async function get<T>(path: string): Promise<T> {
  const resp = await fetch(path, { credentials: 'same-origin' })
  if (resp.status === 401) throw new Unauthorized()
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`)
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}

export function post<T = unknown>(path: string, body?: unknown): Promise<T> {
  return send<T>('POST', path, body)
}

export function patch<T>(path: string, body: unknown): Promise<T> {
  return send<T>('PATCH', path, body)
}

export function del(path: string): Promise<unknown> {
  return send('DELETE', path)
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (resp.status === 401) throw new Unauthorized()
  if (!resp.ok) {
    const detail = await resp.json().then((j) => j.detail).catch(() => resp.statusText)
    throw new Error(typeof detail === 'string' ? detail : `${resp.status}`)
  }
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}
