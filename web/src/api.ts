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
  priority: string | null
  due_at: string | null
  account: string
  account_color: string
}

export type Alert = { kind: string; account: string; message: string }

export type Overview = { urgent: Alert[]; inbox: { count: number; preview: Item[] } }

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

export async function post(path: string): Promise<void> {
  await fetch(path, { method: 'POST', credentials: 'same-origin' })
}

export async function patch<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(path, {
    method: 'PATCH',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (resp.status === 401) throw new Unauthorized()
  if (!resp.ok) {
    const detail = await resp.json().then((j) => j.detail).catch(() => resp.statusText)
    throw new Error(typeof detail === 'string' ? detail : `${resp.status}`)
  }
  return resp.json() as Promise<T>
}
