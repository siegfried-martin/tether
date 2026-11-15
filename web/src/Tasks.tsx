import { useEffect, useState } from 'react'
import { del, patch, post, type Task } from './api'
import { errorText, ToastView, usePolling, useToast, type Toast } from './shared'

function localToday(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T12:00:00`)
  d.setDate(d.getDate() + days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function DueLabel({ due }: { due: string | null }) {
  if (!due) return null
  const day = due.slice(0, 10)
  const today = localToday()
  const pretty = new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  if (day < today) return <span className="due overdue">Overdue · {pretty}</span>
  if (day === today) return <span className="due today">Today</span>
  if (day === addDays(today, 1)) return <span className="due">Tomorrow</span>
  return <span className="due">{pretty}</span>
}

type Done = Task & { warnings: string[]; archived_items: number[] }

export function TasksPage() {
  const open = usePolling<Task[]>('/api/tasks?status=open')
  const [showDone, setShowDone] = useState(false)
  const [doneVersion, setDoneVersion] = useState(0)
  const [editing, setEditing] = useState<number | null>(null)
  const [hidden, setHidden] = useState<Set<number>>(new Set())
  const [toast, setToast] = useToast()

  const refresh = () => { open.reload(); setDoneVersion((v) => v + 1) }
  const fail = (e: unknown) => setToast({ text: errorText(e), error: true })

  const complete = async (task: Task) => {
    setHidden((h) => new Set(h).add(task.id))
    try {
      const result = await post<Done>(`/api/tasks/${task.id}/complete`)
      const undo = () => { setToast(null); post(`/api/tasks/${task.id}/reopen`).then(refresh, fail) }
      const t: Toast = result.warnings.length ? { text: result.warnings.join(' '), error: true, undo }
        : { text: result.archived_items.length ? 'Done, and its email was archived' : 'Done', undo }
      setToast(t)
    } catch (e) {
      fail(e)
    } finally {
      setHidden((h) => { const n = new Set(h); n.delete(task.id); return n })
      refresh()
    }
  }

  const reopen = (task: Task) => post(`/api/tasks/${task.id}/reopen`).then(refresh, fail)

  const rows = open.data?.filter((t) => !hidden.has(t.id) && !(t.parent_id && hidden.has(t.parent_id)))

  return (
    <main className="tasks-page">
      <AddTask onAdded={refresh} onError={fail} placeholder="Add a task" />
      {open.error && <p className="error">Couldn't load tasks: {open.error}</p>}
      {rows?.length === 0 && <p className="muted">No open tasks. Make one from an email in the inbox, or above.</p>}
      <ul className="tasks">
        {rows?.map((t) => (
          <TaskRow key={t.id} task={t} editing={editing === t.id}
                   onToggleEdit={() => setEditing(editing === t.id ? null : t.id)}
                   onCheck={() => complete(t)} onChanged={refresh} onError={fail} />
        ))}
      </ul>
      <button className="link done-toggle" onClick={() => setShowDone(!showDone)} aria-expanded={showDone}>
        {showDone ? 'Hide completed' : 'Show completed'}
      </button>
      {showDone && <DoneList version={doneVersion} onReopen={reopen} />}
      <ToastView toast={toast} />
    </main>
  )
}

function DoneList({ version, onReopen }: { version: number; onReopen: (t: Task) => void }) {
  const done = usePolling<Task[]>('/api/tasks?status=done')
  const { reload } = done
  useEffect(() => { if (version) reload() }, [version, reload])
  return (
    <ul className="tasks done">
      {done.data?.length === 0 && <li className="muted">Nothing completed yet.</li>}
      {done.data?.map((t) => (
        <li key={t.id} className="task-row">
          <div className="task-line">
            <input type="checkbox" className="check" checked onChange={() => onReopen(t)}
                   aria-label={`Reopen: ${t.title}`} />
            <span className="task-title">{t.title}</span>
          </div>
        </li>
      ))}
    </ul>
  )
}

function AddTask({ parentId, onAdded, onError, placeholder }: {
  parentId?: number; onAdded: () => void; onError: (e: unknown) => void; placeholder: string
}) {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || busy) return
    setBusy(true)
    try {
      await post('/api/tasks', { title, parent_id: parentId ?? null })
      setTitle('')
      onAdded()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }
  return (
    <form className={parentId ? 'add-task sub' : 'add-task'} onSubmit={submit}>
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={placeholder}
             aria-label={placeholder} enterKeyHint="done" />
      <button className="button" disabled={!title.trim() || busy}>Add</button>
    </form>
  )
}

function TaskRow({ task, editing, onToggleEdit, onCheck, onChanged, onError }: {
  task: Task; editing: boolean; onToggleEdit: () => void; onCheck: () => void
  onChanged: () => void; onError: (e: unknown) => void
}) {
  return (
    <li className={`task-row${task.parent_id ? ' subtask' : ''}${editing ? ' editing' : ''}`}>
      <div className="task-line">
        <input type="checkbox" className="check" checked={false} onChange={onCheck}
               aria-label={`Complete: ${task.title}`} />
        <button className="task-title" onClick={onToggleEdit} aria-expanded={editing}>{task.title}</button>
        <span className="task-meta">
          {task.priority === 'high' && <span className="flag">High</span>}
          {task.notes && <span className="muted" title={task.notes}>✎</span>}
          <DueLabel due={task.due_at} />
        </span>
      </div>
      {task.item_id && (
        <a className="task-link" href={task.item_url ?? undefined} target="_blank" rel="noreferrer"
           title={task.item_account ?? ''}>
          <span className="dot" style={{ background: task.item_color ?? 'var(--muted)' }} />
          <span className="grow">{task.item_title || '(no subject)'}</span>
          {task.item_state === 'archived' && <span className="muted">archived</span>}
        </a>
      )}
      {editing && <TaskEditor task={task} onChanged={onChanged} onError={onError} />}
    </li>
  )
}

function TaskEditor({ task, onChanged, onError }: { task: Task; onChanged: () => void; onError: (e: unknown) => void }) {
  const [title, setTitle] = useState(task.title)
  const [notes, setNotes] = useState(task.notes ?? '')
  useEffect(() => { setTitle(task.title); setNotes(task.notes ?? '') }, [task.title, task.notes])

  const save = (fields: Partial<Task>) =>
    patch(`/api/tasks/${task.id}`, fields).then(onChanged, (e) => { onError(e); onChanged() })

  const remove = () => {
    if (!confirm(`Delete "${task.title}"${task.parent_id ? '' : ' and its subtasks'}?`)) return
    del(`/api/tasks/${task.id}`).then(onChanged, onError)
  }

  const today = localToday()
  return (
    <div className="task-editor">
      <input className="edit-title" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Title"
             onBlur={() => title.trim() && title !== task.title && save({ title })} />
      <textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Notes" rows={3}
                aria-label="Notes" onBlur={() => notes !== (task.notes ?? '') && save({ notes: notes || null })} />
      <div className="editor-row">
        <label>Due
          <input type="date" value={task.due_at ?? ''} onChange={(e) => save({ due_at: e.target.value || null })} />
        </label>
        <span className="quick-dates">
          <button className="link" onClick={() => save({ due_at: today })}>Today</button>
          <button className="link" onClick={() => save({ due_at: addDays(today, 1) })}>Tomorrow</button>
          {task.due_at && <button className="link" onClick={() => save({ due_at: null })}>Clear</button>}
        </span>
        <label>Priority
          <select value={task.priority ?? ''} onChange={(e) => save({ priority: (e.target.value || null) as Task['priority'] })}>
            <option value="">None</option>
            <option value="high">High</option>
            <option value="normal">Normal</option>
            <option value="low">Low</option>
          </select>
        </label>
      </div>
      {!task.parent_id && <AddTask parentId={task.id} onAdded={onChanged} onError={onError} placeholder="Add a subtask" />}
      <div className="editor-row">
        <button className="link danger" onClick={remove}>Delete task</button>
      </div>
    </div>
  )
}
