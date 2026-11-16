import { useEffect, useState } from 'react'
import { del, patch, post, type Task, type TaskPage } from './api'
import { accountStyle, addDays, DueLabel, errorText, localToday, PAGE_SIZE, Pager, pageParam, relativeTime, ToastView, usePolling, useToast, type Toast } from './shared'
import { ExternalIcon, externalName, ReadingPane } from './Mail'
import { navigate, withParams, type Location } from './route'

type Done = Task & { warnings: string[]; archived_items: number[] }

export function TasksPage({ loc }: { loc: Location }) {
  const page = pageParam(loc.params)
  const list = usePolling<TaskPage>(`/api/tasks/page?offset=${(page - 1) * PAGE_SIZE}&limit=${PAGE_SIZE}`)
  const [editing, setEditing] = useState<number | null>(null)
  const [reading, setReading] = useState<number | null>(null)
  // Checked at once, before the server answers.
  const [flipped, setFlipped] = useState<Set<number>>(new Set())
  const [toast, setToast] = useToast()

  const refresh = list.reload
  const fail = (e: unknown) => setToast({ text: errorText(e), error: true })
  const flip = (id: number, on: boolean) =>
    setFlipped((cur) => { const next = new Set(cur); if (on) next.add(id); else next.delete(id); return next })

  const complete = async (task: Task) => {
    flip(task.id, true)
    try {
      const result = await post<Done>(`/api/tasks/${task.id}/complete`)
      const undo = () => { setToast(null); post(`/api/tasks/${task.id}/reopen`).then(refresh, fail) }
      const t: Toast = result.warnings.length ? { text: result.warnings.join(' '), error: true, undo }
        : { text: result.archived_items.length ? 'Done, and its email was archived' : 'Done', undo }
      setToast(t)
    } catch (e) {
      fail(e)
    } finally {
      flip(task.id, false)
      refresh()
    }
  }

  const reopen = async (task: Task) => {
    flip(task.id, true)
    try { await post(`/api/tasks/${task.id}/reopen`) } catch (e) { fail(e) } finally { flip(task.id, false); refresh() }
  }

  const rows = list.data?.tasks
  const firstDone = rows?.findIndex((t) => t.status === 'done') ?? -1

  return (
    <main className="tasks-page">
      <AddTask onAdded={refresh} onError={fail} placeholder="Add a task" />
      {list.error && <p className="error">Couldn't load tasks: {list.error}</p>}
      {list.data && list.data.open === 0 && page === 1 && (
        <p className="muted">No open tasks. Make one from an email in the inbox, or above.</p>
      )}
      <ul className="tasks">
        {rows?.map((t, n) => (
          <TaskRow key={t.id} task={t} first={n === firstDone} editing={editing === t.id} reading={reading === t.id}
                   checked={(t.status === 'done') !== flipped.has(t.id)}
                   onToggleEdit={() => setEditing(editing === t.id ? null : t.id)}
                   onToggleRead={() => setReading(reading === t.id ? null : t.id)}
                   onCheck={() => (t.status === 'done' ? reopen(t) : complete(t))} onChanged={refresh} onError={fail} />
        ))}
      </ul>
      {list.data && <Pager page={page} total={list.data.total} onPage={(n) => navigate(withParams({ page: n > 1 ? n : null }))} />}
      <ToastView toast={toast} />
    </main>
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

function TaskRow({ task, first, editing, reading, checked, onToggleEdit, onToggleRead, onCheck, onChanged, onError }: {
  task: Task; first: boolean; editing: boolean; reading: boolean; checked: boolean
  onToggleEdit: () => void; onToggleRead: () => void; onCheck: () => void
  onChanged: () => void; onError: (e: unknown) => void
}) {
  const done = task.status === 'done'
  return (
    <li className={`task-row${task.parent_id ? ' subtask' : ''}${editing ? ' editing' : ''}${done ? ' done' : ''}${first ? ' first-done' : ''}`}>
      {first && <h2 className="done-head">Completed</h2>}
      <div className="task-line">
        <input type="checkbox" className="check" checked={checked} onChange={onCheck}
               aria-label={`${done ? 'Reopen' : 'Complete'}: ${task.title}`} />
        <button className="task-title" onClick={onToggleEdit} aria-expanded={editing} disabled={done}>{task.title}</button>
        <span className="task-meta">
          {done ? task.completed_at && <span className="muted due">{relativeTime(task.completed_at)}</span> : <>
            {task.priority === 'high' && <span className="flag">High</span>}
            {task.notes && <span className="muted" title={task.notes}>✎</span>}
            <DueLabel due={task.due_at} />
          </>}
        </span>
      </div>
      {task.item_id && (
        <div className={`task-link tinted${reading ? ' open' : ''}`} style={accountStyle(task.item_color)}>
          <div className="task-link-line">
            <button className="task-link-open" onClick={onToggleRead} aria-expanded={reading} title={task.item_account ?? ''}>
              <span className="grow">{task.item_title || '(no subject)'}</span>
              {task.item_state === 'archived' && <span className="muted">archived</span>}
            </button>
            {task.item_url && (
              <a className="row-action small" href={task.item_url} target="_blank" rel="noreferrer"
                 title={`Open in ${externalName(task.item_url)}`} aria-label={`Open in ${externalName(task.item_url)}`}>
                <ExternalIcon />
              </a>
            )}
          </div>
          {reading && <ReadingPane itemId={task.item_id} kind={task.item_kind ?? 'email'} url={task.item_url} />}
        </div>
      )}
      {editing && !done && <TaskEditor task={task} onChanged={onChanged} onError={onError} />}
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
