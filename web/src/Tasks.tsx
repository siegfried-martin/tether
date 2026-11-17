import { useEffect, useRef, useState } from 'react'
import { del, patch, post, type Tag, type TagRef, type Task, type TaskPage } from './api'
import { accountStyle, addDays, ColorPicker, DueLabel, errorText, localToday, PAGE_SIZE, Pager, pageParam, relativeTime, ToastView, useMedia, usePolling, useToast, type Toast } from './shared'
import { ExternalIcon, externalName, ReadingPane } from './Mail'
import { navigate, withParams, type Location } from './route'

type Done = Task & { warnings: string[]; archived_items: number[] }
type Drop = { id: number; where: 'before' | 'after' }

/** Drag to reorder needs a mouse or trackpad and room for the handle: not on phones. */
const CAN_DRAG = '(min-width: 481px) and (pointer: fine)'

export function TasksPage({ loc }: { loc: Location }) {
  const page = pageParam(loc.params)
  const tagId = Number(loc.params.get('tag')) || null
  const list = usePolling<TaskPage>(`/api/tasks/page?offset=${(page - 1) * PAGE_SIZE}&limit=${PAGE_SIZE}${tagId ? `&tag=${tagId}` : ''}`)
  const tags = usePolling<Tag[]>('/api/tags')
  const filter = tags.data?.find((t) => t.id === tagId) ?? null
  const filterBy = (id: number | null) => navigate(withParams({ tag: id, page: null }))
  const [editing, setEditing] = useState<number | null>(null)
  const [reading, setReading] = useState<number | null>(null)
  // Checked at once, before the server answers.
  const [flipped, setFlipped] = useState<Set<number>>(new Set())
  const [toast, setToast] = useToast()
  const canDrag = useMedia(CAN_DRAG)
  // A move shows at once; the next answer from the server replaces it.
  const [moved, setMoved] = useState<{ base: TaskPage; rows: Task[] } | null>(null)
  const [dragging, setDragging] = useState<number | null>(null)
  const [drop, setDrop] = useState<Drop | null>(null)

  const refresh = () => { list.reload(); tags.reload() }
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

  const rows = moved && moved.base === list.data ? moved.rows : list.data?.tasks
  const firstDone = rows?.findIndex((t) => t.status === 'done') ?? -1

  const move = (task: Task, to: Drop) => {
    if (!rows || !list.data) return
    const next = reorder(rows, task, to)
    if (next.every((t, n) => t.id === rows[n].id)) return
    setMoved({ base: list.data, rows: next })
    post(`/api/tasks/${task.id}/move`, to.where === 'before' ? { before: to.id } : { after: to.id })
      .catch(fail).finally(refresh)
  }

  /** The task a drag over this row would drop next to, and on which side. */
  const dropAt = (e: React.DragEvent): Drop | null => {
    const dragged = rows?.find((t) => t.id === dragging)
    const li = (e.target as Element).closest<HTMLElement>('li[data-task]')
    const over = rows?.find((t) => t.id === Number(li?.dataset.task))
    if (!dragged || !over || !rows) return null
    // A task moves past whole tasks, subtasks and all; a subtask moves among its siblings.
    const target = dragged.parent_id === null && over.parent_id !== null
      ? rows.find((t) => t.id === over.parent_id) : over
    if (!target || target.id === dragged.id || target.status !== 'open' || target.parent_id !== dragged.parent_id) return null
    const block = [...document.querySelectorAll<HTMLElement>(
      target.parent_id === null ? `li[data-block="${target.id}"]` : `li[data-task="${target.id}"]`)]
    const top = block[0].getBoundingClientRect().top
    const bottom = block[block.length - 1].getBoundingClientRect().bottom
    return { id: target.id, where: e.clientY < (top + bottom) / 2 ? 'before' : 'after' }
  }
  const dragProps = canDrag && dragging !== null ? {
    onDragOver: (e: React.DragEvent) => {
      const at = dropAt(e)
      if (at) e.preventDefault()
      if (at?.id !== drop?.id || at?.where !== drop?.where) setDrop(at)
    },
    onDragLeave: (e: React.DragEvent) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDrop(null)
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault()
      const task = rows?.find((t) => t.id === dragging)
      const at = dropAt(e)
      if (task && at) move(task, at)
      setDrop(null)
    },
  } : {}
  // With keys: up and down past the neighboring task (or subtask).
  const nudge = (task: Task, step: -1 | 1) => {
    const siblings = rows?.filter((t) => t.status === 'open' && t.parent_id === task.parent_id) ?? []
    const other = siblings[siblings.findIndex((t) => t.id === task.id) + step]
    if (other) move(task, { id: other.id, where: step < 0 ? 'before' : 'after' })
  }
  const blockEnd = (id: number) => {
    const inBlock = rows?.filter((t) => t.id === id || t.parent_id === id) ?? []
    return inBlock[inBlock.length - 1]?.id
  }

  return (
    <main className="tasks-page">
      <AddTask onAdded={refresh} onError={fail} tags={filter ? [filter.name] : []}
               placeholder={filter ? `Add a task tagged ${filter.name}` : 'Add a task'} />
      {!!tags.data?.length && <TagFilter tags={tags.data} selected={tagId} onSelect={filterBy} />}
      {list.error && <p className="error">Couldn't load tasks: {list.error}</p>}
      {list.data && list.data.open === 0 && page === 1 && (
        <p className="muted">{filter ? `No open tasks tagged ${filter.name}.`
          : 'No open tasks. Make one from an email in the inbox, or above.'}</p>
      )}
      <ul className={`tasks${dragging !== null ? ' sorting' : ''}`} {...dragProps}>
        {rows?.map((t, n) => (
          <TaskRow key={t.id} task={t} first={n === firstDone} editing={editing === t.id} reading={reading === t.id}
                   block={t.parent_id !== null && rows.some((p) => p.id === t.parent_id && p.status === 'open') ? t.parent_id : t.id}
                   drag={canDrag ? {
                     dragging: dragging !== null && (t.id === dragging || t.parent_id === dragging),
                     drop: drop?.where === 'before' && drop.id === t.id ? 'before'
                       : drop?.where === 'after' && blockEnd(drop.id) === t.id ? 'after' : null,
                     onStart: () => setDragging(t.id),
                     onEnd: () => { setDragging(null); setDrop(null) },
                     onNudge: (step) => nudge(t, step),
                   } : null}
                   checked={(t.status === 'done') !== flipped.has(t.id)}
                   onToggleEdit={() => setEditing(editing === t.id ? null : t.id)}
                   onToggleRead={() => setReading(reading === t.id ? null : t.id)}
                   onCheck={() => (t.status === 'done' ? reopen(t) : complete(t))} onChanged={refresh} onError={fail}
                   allTags={tags.data ?? []} onTag={(id) => filterBy(id === tagId ? null : id)} />
        ))}
      </ul>
      {list.data && <Pager page={page} total={list.data.total} onPage={(n) => navigate(withParams({ page: n > 1 ? n : null }))} />}
      <ToastView toast={toast} />
    </main>
  )
}

function AddTask({ parentId, onAdded, onError, placeholder, tags = [] }: {
  parentId?: number; onAdded: () => void; onError: (e: unknown) => void; placeholder: string; tags?: string[]
}) {
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || busy) return
    setBusy(true)
    try {
      await post('/api/tasks', { title, parent_id: parentId ?? null, tags })
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

/** rows with task moved just before or after the task to.id, a task taking its subtasks along. */
function reorder(rows: Task[], task: Task, to: Drop): Task[] {
  const inBlock = (t: Task, id: number) => t.id === id || (task.parent_id === null && t.parent_id === id)
  const block = rows.filter((t) => inBlock(t, task.id))
  const rest = rows.filter((t) => !inBlock(t, task.id))
  const targets = rest.map((t, n) => (inBlock(t, to.id) ? n : -1)).filter((n) => n >= 0)
  const at = to.where === 'before' ? targets[0] : targets[targets.length - 1] + 1
  return [...rest.slice(0, at), ...block, ...rest.slice(at)]
}

type DragRow = {
  dragging: boolean; drop: 'before' | 'after' | null
  onStart: () => void; onEnd: () => void; onNudge: (step: -1 | 1) => void
}

function TaskRow({ task, first, block, drag, editing, reading, checked, onToggleEdit, onToggleRead, onCheck, onChanged, onError, allTags, onTag }: {
  task: Task; first: boolean; block: number; drag: DragRow | null; editing: boolean; reading: boolean; checked: boolean
  onToggleEdit: () => void; onToggleRead: () => void; onCheck: () => void
  onChanged: () => void; onError: (e: unknown) => void; allTags: Tag[]; onTag: (id: number) => void
}) {
  const done = task.status === 'done'
  const row = useRef<HTMLLIElement>(null)
  const classes = ['task-row', task.parent_id && 'subtask', editing && 'editing', done && 'done', first && 'first-done',
                   drag?.dragging && 'dragging', drag?.drop && `drop-${drag.drop}`]
  return (
    <li ref={row} className={classes.filter(Boolean).join(' ')} data-task={task.id} data-block={block}>
      {first && <h2 className="done-head">Completed</h2>}
      <div className="task-line">
        {drag && (done ? <span className="drag-handle" aria-hidden /> : (
          // A span, not a button: Firefox won't start a drag from a button.
          <span role="button" tabIndex={0} className="drag-handle" draggable title="Drag to reorder (or focus and use ↑ ↓)"
                  aria-label={`Reorder ${task.title}: use the up and down arrow keys`}
                  onDragStart={(e) => {
                    e.dataTransfer.effectAllowed = 'move'
                    e.dataTransfer.setData('text/plain', task.title)
                    if (row.current) e.dataTransfer.setDragImage(row.current, 12, 20)
                    drag.onStart()
                  }}
                  onDragEnd={drag.onEnd}
                  onKeyDown={(e) => {
                    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
                    e.preventDefault()
                    drag.onNudge(e.key === 'ArrowUp' ? -1 : 1)
                  }}>
            <GripIcon />
          </span>
        ))}
        <input type="checkbox" className="check" checked={checked} onChange={onCheck}
               aria-label={`${done ? 'Reopen' : 'Complete'}: ${task.title}`} />
        <button className="task-title" onClick={onToggleEdit} aria-expanded={editing} disabled={done}>{task.title}</button>
        {task.tags.length > 0 && (
          <span className="task-tags">
            {task.tags.slice(0, MAX_ROW_TAGS).map((t) => (
              <TagChip key={t.id} tag={t} onClick={() => onTag(t.id)} title={`Show tasks tagged ${t.name}`} />
            ))}
            {task.tags.length > MAX_ROW_TAGS && (
              <span className="muted tag-more" title={task.tags.slice(MAX_ROW_TAGS).map((t) => t.name).join(', ')}>
                +{task.tags.length - MAX_ROW_TAGS}
              </span>
            )}
          </span>
        )}
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
      {editing && !done && <TaskEditor task={task} allTags={allTags} onChanged={onChanged} onError={onError} />}
    </li>
  )
}

function TaskEditor({ task, allTags, onChanged, onError }: {
  task: Task; allTags: Tag[]; onChanged: () => void; onError: (e: unknown) => void
}) {
  const [title, setTitle] = useState(task.title)
  const [notes, setNotes] = useState(task.notes ?? '')
  useEffect(() => { setTitle(task.title); setNotes(task.notes ?? '') }, [task.title, task.notes])

  const save = (fields: Partial<Omit<Task, 'tags'>> | { tags: string[] }) =>
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
      <TagPicker tags={task.tags} allTags={allTags} onChange={(tags) => save({ tags })} />
      {!task.parent_id && <AddTask parentId={task.id} onAdded={onChanged} onError={onError} placeholder="Add a subtask" />}
      <div className="editor-row">
        <button className="link danger" onClick={remove}>Delete task</button>
      </div>
    </div>
  )
}

function GripIcon() {
  return (
    <svg width="10" height="16" viewBox="0 0 10 16" aria-hidden="true" fill="currentColor">
      {[2, 8, 14].flatMap((y) => [2, 8].map((x) => <circle key={`${x}${y}`} cx={x} cy={y} r="1.5" />))}
    </svg>
  )
}

// ------------------------------------------------------------------ tags

const MAX_ROW_TAGS = 2

function TagChip({ tag, onClick, onRemove, title, pressed }: {
  tag: TagRef; onClick?: () => void; onRemove?: () => void; title?: string; pressed?: boolean
}) {
  const body = <span className="tag-name">{tag.name}</span>
  return (
    <span className={`tag-chip${pressed ? ' pressed' : ''}`} style={accountStyle(tag.color)}>
      {onClick ? <button type="button" onClick={onClick} title={title} aria-pressed={pressed}>{body}</button> : body}
      {onRemove && (
        <button type="button" className="tag-remove" onClick={onRemove} aria-label={`Remove tag ${tag.name}`}>×</button>
      )}
    </span>
  )
}

/** The tags along the top of the Tasks page: tap one to see only its tasks, again (or ×) to see all. */
function TagFilter({ tags, selected, onSelect }: { tags: Tag[]; selected: number | null; onSelect: (id: number | null) => void }) {
  return (
    <div className="tag-filter" role="group" aria-label="Show tasks tagged">
      {tags.map((t) => (
        <TagChip key={t.id} tag={t} pressed={t.id === selected} onClick={() => onSelect(t.id === selected ? null : t.id)}
                 title={t.id === selected ? 'Show all tasks' : `Show tasks tagged ${t.name}`} />
      ))}
      {selected !== null && <button className="link" onClick={() => onSelect(null)}>Show all</button>}
    </div>
  )
}

/** A task's tags, with a box to add one: pick an existing tag, or type a new name and press Enter. */
function TagPicker({ tags, allTags, onChange }: { tags: TagRef[]; allTags: Tag[]; onChange: (names: string[]) => void }) {
  const [text, setText] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const names = tags.map((t) => t.name)
  const query = text.trim().replace(/^#/, '').toLowerCase()
  const choices = allTags.filter((t) => !names.some((n) => n.toLowerCase() === t.name.toLowerCase())
                                        && t.name.toLowerCase().includes(query)).slice(0, 8)
  const exact = allTags.some((t) => t.name.toLowerCase() === query) || names.some((n) => n.toLowerCase() === query)
  const options: { label: string; name: string; tag?: Tag }[] = [
    ...choices.map((t) => ({ label: t.name, name: t.name, tag: t })),
    ...(query && !exact ? [{ label: `Create tag “${text.trim().replace(/^#/, '')}”`, name: text.trim().replace(/^#/, '') }] : []),
  ]
  const add = (name: string) => {
    if (!name.trim()) return
    onChange([...names, name.trim()])
    setText(''); setActive(0)
    input.current?.focus()
  }
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      const pick = options[active] ?? (query ? { name: text.trim() } : null)
      if (pick) add(pick.name)
    } else if (e.key === 'ArrowDown') {
      e.preventDefault(); setActive((a) => Math.min(a + 1, options.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault(); setActive((a) => Math.max(a - 1, 0))
    } else if (e.key === 'Backspace' && !text && names.length) {
      onChange(names.slice(0, -1))
    } else if (e.key === 'Escape' && (text || open)) {
      e.stopPropagation(); setText(''); setOpen(false)
    }
  }
  return (
    <div className="tag-picker">
      <span className="tag-picker-label">Tags</span>
      {tags.map((t) => <TagChip key={t.id} tag={t} onRemove={() => onChange(names.filter((n) => n !== t.name))} />)}
      <span className="tag-input">
        <input ref={input} value={text} placeholder={tags.length ? 'Add another' : 'Add a tag'} aria-label="Add a tag"
               role="combobox" aria-expanded={open && options.length > 0} aria-controls="tag-options" aria-autocomplete="list"
               onChange={(e) => { setText(e.target.value); setActive(0); setOpen(true) }}
               onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onKeyDown={onKey} enterKeyHint="done" />
        {open && options.length > 0 && (
          <ul className="tag-options" id="tag-options" role="listbox">
            {options.map((o, n) => (
              <li key={o.label} role="option" aria-selected={n === active}>
                {/* mousedown, so the input keeps focus and the list stays open for another pick */}
                <button type="button" tabIndex={-1} className={n === active ? 'active' : ''}
                        onMouseDown={(e) => { e.preventDefault(); add(o.name) }}>
                  {o.tag ? <><span className="swatch" style={{ background: o.tag.color }} />{o.label}</> : o.label}
                </button>
              </li>
            ))}
          </ul>
        )}
      </span>
    </div>
  )
}

/** Settings: every tag, to recolor, rename or delete, and a box to make a new one. */
export function TagSettings() {
  const tags = usePolling<Tag[]>('/api/tags')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const fail = (e: unknown) => setError(errorText(e))
  const create = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    post('/api/tags', { name }).then(() => { setName(''); setError(null); tags.reload() }, fail)
  }
  return (
    <section>
      <h2>Task tags</h2>
      {tags.data?.length === 0 && <p className="muted">No tags yet. Add one here, or from a task.</p>}
      <ul className="tag-list">
        {tags.data?.map((t) => <TagSetting key={t.id} tag={t} onChanged={() => { setError(null); tags.reload() }} onError={fail} />)}
      </ul>
      <form className="add-task" onSubmit={create}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New tag" aria-label="New tag" maxLength={40} />
        <button className="button" disabled={!name.trim()}>Add</button>
      </form>
      {error && <p className="error">{error}</p>}
    </section>
  )
}

function TagSetting({ tag, onChanged, onError }: { tag: Tag; onChanged: () => void; onError: (e: unknown) => void }) {
  const [name, setName] = useState(tag.name)
  useEffect(() => setName(tag.name), [tag.name])
  const rename = () => {
    if (!name.trim() || name.trim() === tag.name) { setName(tag.name); return }
    patch(`/api/tags/${tag.id}`, { name }).then(onChanged, (e) => { setName(tag.name); onError(e) })
  }
  const remove = () => {
    const used = tag.open_tasks + tag.done_tasks
    if (used && !confirm(`Delete the tag "${tag.name}"? It comes off ${used} task${used === 1 ? '' : 's'}; the tasks stay.`)) return
    del(`/api/tags/${tag.id}`).then(onChanged, onError)
  }
  return (
    <li className="tag-setting">
      <ColorPicker value={tag.color} label={`Color for tag ${tag.name}`}
                   save={(color) => patch(`/api/tags/${tag.id}`, { color }).then(onChanged)} />
      <input value={name} onChange={(e) => setName(e.target.value)} aria-label={`Name of tag ${tag.name}`} maxLength={40}
             onBlur={rename} onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); if (e.key === 'Escape') setName(tag.name) }} />
      <span className="muted">{tag.open_tasks} open</span>
      <button className="link danger" onClick={remove}>Delete</button>
    </li>
  )
}
