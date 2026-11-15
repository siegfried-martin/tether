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
  note?: string
}

export type Alert = { kind: string; account: string; message: string }

export type Overview = {
  urgent: Alert[]
  inbox: { count: number; by_account: Record<string, number>; preview: Item[] }
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
}

export class Unauthorized extends Error {}

export async function get<T>(path: string): Promise<T> {
  const resp = await fetch(path, { credentials: 'same-origin' })
  if (resp.status === 401) throw new Unauthorized()
  if (!resp.ok) throw new Error(`${path}: ${resp.status}`)
  return resp.json() as Promise<T>
}

export function post<T = unknown>(path: string, body?: unknown): Promise<T> {
  return send<T>('POST', path, body)
}

export function patch<T>(path: string, body: unknown): Promise<T> {
  return send<T>('PATCH', path, body)
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
  return resp.json() as Promise<T>
}
