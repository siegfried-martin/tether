# Tether — Design Spec v1.2

This file is the source of truth for the design. Where other notes conflict with it, the spec wins.

Tether is a self-hosted, single-user aggregator: several email inboxes, calendars and a task list in one triage surface, with an MCP connector so Claude can read mail and manage tasks on the owner's behalf. Everything deployment-specific (public URL, port, accounts, secrets) is configuration, not code.

**Background.** Tether was first built for Ziggy, a director at Stewart Title. That is where two of its features come from. The work digest covers a work mailbox with no API access. The ICS calendar source covers a work calendar that can only be published as a feed.

## Decisions

| Call | Decision |
| --- | --- |
| Hosting | One process and one SQLite file on a small Linux server, as its own system user, behind a reverse proxy with TLS (nginx and certbot). Public URL, port and deploy target come from environment files. `deploy/deploy.sh` builds and installs it. |
| Work calendar publish level | "Titles and locations". |
| Follow-up creation | Manual pin and auto-resolve only. The model pass over Sent is designed below but not approved, and is not built until the owner approves it. |
| Digest waiting-on | No `WAITING` block in the digest. The parser keeps keyword dispatch so one can be added later. |
| Digest data | Digest items (sender, subject, needed, due, priority, link, received time) are stored on the server, with no purge rule. |
| Backups | Out of scope. Rely on the host's snapshots. Provider data re-syncs after a restore. Tasks, follow-ups and archive state exist only in Tether, so a restore loses what changed since the last snapshot. |
| Mail accounts | Any number of Google mailboxes, connected over OAuth. A mailbox with no API access comes in through the digest. |
| Claude reading mail | Claude can search every connected mailbox and read threads through MCP, so it can build the to-do list from every inbox. Mail content reaches Claude only when Claude calls `search_mail` or `get_thread`. The server-side model pass stays unapproved. |
| Claude acting on your behalf | Connecting Claude is your consent. Claude may create and change tasks when you ask, or under a standing instruction you gave it (for example a Claude memory saying "whenever I do X, also create a task"). It does not need a per-request ask. |
| MVP scope | Email, calendar and tasks: the Gmail accounts, the work digest, follow-ups on Gmail, the calendar hub, and tasks. Slack and GroupMe are post-MVP; their design stays below so it is not lost. |

## Design rules

Seven rules settle most questions the sections below would otherwise reopen.

- **Providers are the source of truth.** The app stores metadata, a snippet and a deep link. It stores no message bodies: when Claude reads a thread, the body is fetched live from the provider, returned, and never stored or logged.
- **An item is a conversation, not a message.** New inbound activity reopens it.
- **Every write is an idempotent upsert on a natural key.** Rerunning a sync or re-pasting a digest changes nothing.
- **One service layer.** REST and MCP are thin adapters over the same functions, with no logic in either.
- **Rules decide; the model only proposes.** The server never lets model output change state on its own: a deterministic rule or your consent does. A Claude tool call counts as your consent, whether you asked for it in the moment or set a standing instruction.
- **Failures surface as one urgent alert.** Sync timestamps stay off the main view.
- **No queue, broker, cache or containers.** One process and one SQLite file, until a problem you actually have says otherwise.

Not built but not foreclosed: snooze (one nullable column), reply (items already carry provider IDs), more sources (a new account kind), the model pass over Sent (its column already exists).

## Data model

Nine tables, single user, no tenancy. Each synced table has a natural key so every connector write is an upsert.

| Table | Fields | Unique key |
| --- | --- | --- |
| `account` | id, kind (gmail, slack, groupme, digest, ics), label, color, status, excluded\_categories and initial\_sync\_days (Gmail only) | label |
| `credential` | id, account\_id, ciphertext, scopes, expires\_at, updated\_at | account\_id |
| `item` | id, account\_id, external\_id, kind (email, chat, digest), title, sender, snippet, needed, url, last\_activity\_at, last\_direction (in, out), state (open, archived), archived\_through, priority, due\_at, last\_evaluated\_sent\_id, created\_at, updated\_at | account\_id + external\_id |
| `task` | id, parent\_id, title, notes, status (open, done), due\_at, priority, sort\_order, item\_id, completed\_at, created\_at, updated\_at | none |
| `follow_up` | id, item\_id, waiting\_for, sent\_at, due\_at, status (proposed, waiting, replied, dismissed), source (manual, classifier, digest) | one non-terminal row per item (partial unique index) |
| `calendar_source` | id, account\_id, kind (google, ics), external\_ref, enabled | account\_id + external\_ref |
| `source_event` | id, source\_id, uid, instance\_start, start, end, all\_day, title, location, description, modified\_at, content\_hash, hub\_event\_id, missing\_since | source\_id + uid + instance\_start |
| `hub_event` | id, dedup\_key, google\_event\_id, content\_hash, created\_at | dedup\_key |
| `sync_state` | id, account\_id, stream, cursor, last\_attempt\_at, last\_ok\_at, failures, last\_error | account\_id + stream |

### Item rules

- `external_id` by source: Gmail thread ID; Slack channel ID for DMs, `channel:thread_ts` for mentions (a top-level mention uses its own `ts`); GroupMe group ID; a content hash for digest items.
- State is open or archived, nothing else.
- Unarchive reopens an item. For Gmail it adds INBOX back to the thread; for chat and digest items it is local.
- Chat items have no provider inbox. Archive sets `archived_through` to the last activity time, and any later inbound message reopens the item.

### Gmail inbox membership

- A thread is in when any of its INBOX messages carries a category outside the account's `excluded_categories`. A message with no category label counts as Personal.
- `excluded_categories` defaults to Promotions, Social and Forums, so Personal and Updates are kept. It is set per account in Settings. Changing it triggers a full reconcile for that account. Excluding a category archives its threads in Tether only, never in Gmail.
- A Gmail item is open exactly when its thread is in, in both directions. Archiving in the app removes INBOX from the whole thread. Archiving in Gmail, or moving the thread to an excluded category, archives the item.
- Sent-only threads: sent activity on a thread with no item creates the item as archived, with `last_direction=out`. On an existing item, sent activity updates `last_direction` and `last_activity_at` without changing state. A thread you started can therefore carry a follow-up before any reply arrives.

### Initial sync

- An account's first sync imports only threads and conversations with activity in the last `initial_sync_days`: 1 to 90, set per account in Settings, defaulting to the config value (14). Older threads appear when they get new activity.
- Changing the window triggers a full reconcile, which imports threads newly inside it. Shrinking it removes nothing: the window limits how far back imports reach, and the inbox still mirrors Gmail.
- For Gmail this means INBOX threads. Slack and GroupMe backfill the same window.

### Task rules

- Two levels, enforced in the service: a task with a parent cannot be a parent.
- Completing a parent completes its open children.
- Completing a task archives its linked item once no other open task links to that item.
- For a digest item, archive is local only. Nothing is written back to the source mailbox.

### Stored content

Calendar descriptions are the only body text stored, because merging needs them. They are bounded by the rolling window in the calendar section.

## Connectors

Everything polls except Slack (post-MVP), which holds a socket. Polling needs no public webhook endpoint and no subscription renewals.

| Source | Auth | Sync | Item key |
| --- | --- | --- | --- |
| Gmail, any number of Google mailboxes | One OAuth app in Production. `gmail.modify`, `calendar.readonly` and `calendar.events` on every account | Poll `history.list` from the stored history ID every 60 s | Thread ID |
| Slack workspace (post-MVP) | Internal app, Socket Mode: an app-level token plus your user token | Events over the socket; backfill `conversations.history` from the cursor on reconnect | Channel ID, or `channel:thread_ts` |
| GroupMe group (post-MVP) | Access token | Poll the group's messages with `since_id` every 60 s | Group ID |
| Work mailbox with no API access | None | Manual `ingest_digest` only | Content hash |
| Google calendars | Same OAuth grant | Fetch the full window with `events.list` (`singleEvents=true`) every 5 min and diff | See calendar hub |
| Published ICS calendar (for example a work Outlook calendar) | Published ICS URL, held as a secret | Fetch the full feed every 15 min and diff | See calendar hub |

### Gmail

- `gmail.modify` is the minimum scope, because archiving changes labels. It also covers reading and writing drafts, so no separate compose scope is requested.
- `calendar.events` is requested on every account at connect time, so future event writes from Claude need no reconnect. The hub still writes only to the hub account.
- The OAuth app is for personal use and is not submitted for verification, so Google shows the "unverified app" screen at consent. This is expected. Google's verification banner and its URL checks only apply to a review submission, which is never made. The app serves a privacy page at `/privacy.html` for the consent screen.
- First sync: record the mailbox history ID first, then import INBOX threads inside the initial-sync window, then poll history from the recorded ID. Changes made during the import are replayed, not lost.
- An expired history ID triggers a full reconcile for that account. List every INBOX thread ID, archive open items that are no longer in, import new threads only if they fall inside the initial-sync window, then resume from the current history ID.
- Sent messages arrive in the same history stream. They set `last_direction`, apply the sent-only rule, and resolve follow-ups.
- Pub/Sub push is rejected: it adds a cloud topic, a public endpoint and a watch that must be renewed, to save under a minute.

### Slack (post-MVP)

- A user token is required. A bot token cannot read your DMs with other people.
- Socket Mode also needs an app-level token (`connections:write`).
- Items are created for DMs, group DMs, @mentions of you, and replies in threads you have posted in. All other channel traffic is ignored.

### GroupMe (post-MVP)

- The whole group is one rolling item: snippet is the latest message, with a count since `archived_through`.
- Push is deferred. Polling one group every 60 s is enough, and it reuses the poller that already exists.

### Failure handling

Every stream records its attempt in `sync_state`. A revoked token sets the account status to auth error and raises one urgent alert; other accounts keep syncing.

## Work digest: the ingest\_digest contract

Some mailboxes have no API Tether can use, such as a corporate Microsoft 365 tenant that blocks third-party apps. For those, an assistant inside that environment (Copilot, for example) writes a structured digest of what needs action. You paste it to Claude, and Claude calls `ingest_digest`. The digest goes into a `digest` account.

Claude passes the pasted digest through verbatim and the server parses it. The model never restructures the text, which is what keeps ingestion deterministic.

```python
ingest_digest(raw_text: str, digest_date: date | None = None)
# returns {"created": int, "duplicates": int, "rejected": [{"block": str, "reason": str}]}
```

### Parsing

- The parser anchors on labels, not line breaks. It splits on `ITEM`, then captures each field up to the next label.
- That survives Teams collapsing the digest into one paragraph.
- `NO ACTION ITEMS` is valid input and returns zeros.
- A block missing From, Subject or Needed is rejected with a reason. The other blocks still commit.
- Claude shows rejected blocks to you. It does not guess the missing fields.
- The parser dispatches on the block keyword. Only `ITEM` is recognized in v1; a `WAITING` block can be added later without touching `ITEM`.

### Field mapping

| Digest label | Item field | Handling |
| --- | --- | --- |
| From | sender | As written |
| Subject | title | As written |
| Received | last\_activity\_at | Read in the configured time zone (`TETHER_TIMEZONE`); the digest carries no offset |
| Needed | needed | As written |
| Due | due\_at | `none` becomes null |
| Priority | priority | high, normal or low; anything else becomes normal |
| Link | url | `none` becomes null; opens only where you are signed in to the source mailbox |

### Dedup

- `external_id` is a SHA-256 of lowercased From, lowercased Subject, and Received parsed to an ISO 8601 UTC timestamp. Hashing the parsed value, not the raw text, means the same email written in two date formats gets one ID.
- If Received is missing or unparseable, `digest_date` (ISO date) stands in for it.
- An upsert never reopens an archived digest item. An ask you already handled stays handled if it reappears.

### Where items land

Digest items go into the inbox under the digest account, not straight into tasks. That keeps one triage surface, and converting to a task is the same one tap as for any other item.

## Follow-ups

A follow-up is a row tied to a thread where you spoke last. You create it; only an inbound reply or your tap resolves it.

### Creation

1. **Manual pin, built in v1.** A "waiting on this" action on any item, including a sent-only thread, with an optional due date defaulting to two business days.
2. **Model pass over Sent: designed, not approved, not built.** Each new sent Gmail message would get one model call: does it expect a reply, for what, and by when. A yes would create a follow-up with status `proposed`, which you confirm or dismiss. The pass would fetch the message body, keep only the verdict, and store nothing else. It would evaluate only sent messages newer than the item's `last_evaluated_sent_id`, so a full reconcile neither re-evaluates nor skips any. The column ships now and stays null until the pass is approved.

### Resolution

- Gmail and Slack: an inbound message on the thread or conversation sets the follow-up to `replied`. The thread reappears in the inbox through the normal sync.
- GroupMe: no auto-resolve. Any member's message would count as a reply, so GroupMe follow-ups resolve only by your tap.
- A newer sent message on the same thread updates `sent_at` on the existing follow-up. It does not add a row.
- A follow-up past `due_at` goes to the urgent strip. Once the model pass exists, `proposed` ones go there too, labelled as such and dismissible in one tap.

### Coverage

| Source | Follow-ups in v1 |
| --- | --- |
| Gmail | Manual pin, auto-resolve. Model pass only once approved |
| Slack (post-MVP) | Manual pin, auto-resolve |
| GroupMe (post-MVP) | Manual pin, manual resolve |
| Work digest | None; there is no access to Sent, and no `WAITING` block |

## Calendar hub

Every source event maps to exactly one hub event, and any poll can be rerun without creating duplicates.

### Scope

- Sources are the calendars you select on the connected Google accounts, plus any published ICS feeds.
- The hub is one secondary calendar on one chosen Google account (the hub account). Create it by hand once and put its ID in config. It is the only calendar the hub writes to.
- The hub is never a source. The service rejects a calendar source whose ID equals the hub ID.
- The window is 7 days back to 90 days ahead, rolling.
- Recurring series are expanded into single instances inside the window. This avoids translating recurrence rules, exceptions and time zones between systems. The cost is that an event more than 90 days out appears only once the window reaches it.
- An event whose start drops behind the window is pruned from `source_event` without touching its hub event. Ageing out is not a delete.

### Fetching

- Every source is fetched as its full window on every poll, then diffed against `source_event`. There are no sync tokens; one reconcile path and one delete rule cover Google and ICS alike.
- Google: `events.list` with `timeMin`, `timeMax` and `singleEvents=true`, every 5 minutes. The instance key is iCalUID plus original start time.
- ICS: parsed with `icalendar` and expanded with `recurring-ical-events`, which handle RRULE, EXDATE and RECURRENCE-ID overrides. Outlook writes Windows time zone names (`Eastern Standard Time`), so a Windows-to-IANA map resolves TZIDs before expansion.
- ETag is sent when present but not relied on. A content hash of the fetched feed is the fallback: an unchanged hash skips the diff.

### Grouping

Two tests link source events:

1. Same iCalUID and same instance start: the same invite seen on two accounts.
2. Same start, end and normalized title: your rule, for copies with different UIDs. Normalized title means lowercased, trimmed, whitespace collapsed.

Groups are the connected sets under either test, computed over every source event in the window on each poll.

### Hub assignment

Assignment is sticky: a group keeps the hub event its members already point to. Each poll applies these steps in order.

1. **Splits.** If one hub event's members now fall in more than one group, the largest group keeps it. A tie goes to the group holding the lowest UID-plus-instance value. The other groups go on to step 2 without it.
2. **Merges.** If a group's members point to two or more hub events, keep the oldest by `created_at`, repoint the members, and delete the others.
3. **New groups.** A group whose members point to no hub event gets a new one. Its key is the lowest UID-plus-instance value among its members. If that key is already held by another hub event, `#2`, `#3` and so on are appended until it is free. The `hub_event` row is written before the Google insert, so a retry reuses the key.
4. **Leavers.** A member that stops matching leaves its group and is placed by the same steps. The hub event stays while any member remains.

### Merge

- Title, start and end come from the most recently modified member.
- Descriptions that differ are concatenated, each under its source label.
- Location is the first non-empty one.

### Idempotent writes

- The Google event ID is derived from the hub key: SHA-1, encoded as lowercase base32hex. Google accepts client-chosen IDs in that alphabet ([Events: insert](https://developers.google.com/workspace/calendar/v3/reference/events/insert)).
- An insert that returns 409 means the ID exists, possibly as a cancelled event, because Google keeps deleted events. The service fetches the event and patches it with the current content, including `status: confirmed`.
- A content hash is stored on the hub row and in the event's private extended properties. Unchanged hash, no write.

### Deletes

- A source event absent from a successful window fetch gets `missing_since` set. Unless a hold is in force, it is then removed, and a hub event left with no members is deleted.
- Three guards:
  1. Never reconcile after a failed or unparseable fetch.
  2. Never delete past events.
  3. If one fetch would remove more than 25% of a source's future events **and** more than 5 events, hold all of that fetch's deletes for the source and raise one urgent alert.
- During a hold, inserts and updates still apply. Further absent events join the hold. A held event that shows up again in a later fetch has `missing_since` cleared and leaves the hold. When no held events remain, the hold and its alert clear.
- **Release action**, in Settings and as the MCP tool `release_calendar_hold`. `get_overview` lists each held event's title and start, so Claude can show you what a release would remove before calling it:
  - **Apply** removes the held source events and deletes any hub events they leave without members.
  - **Discard** keeps those hub events in Google exactly as they are and stops managing them. Tether removes the held source events and the orphaned `hub_event` rows without touching Google. If the source events come back later, their key derives the same Google ID, the 409 path re-adopts the event, and nothing is duplicated.

### Latency and staleness

- The app's own lag is at most 15 minutes for ICS and 5 for Google.
- Outlook regenerates its published feed on its own schedule, which this design does not control. Measure the real end-to-end lag in the first week against your two-hour bar.
- A source with no successful poll for two hours raises one urgent alert. The full sync table lives in Settings, not on the home view.

### Work calendar content

Publish the work calendar with all details. Descriptions, including Teams join links, pass into the hub under the usual merge rule, so meetings can be joined straight from the hub calendar. The cost is that work meeting text lands in a personal Google calendar. That is an accepted trade, made for usability. If you want less, publish at "Can view titles and locations" instead; no code change is needed.

### Getting an Outlook ICS URL

Done by hand once, on a machine signed in to the work account:

1. Open Outlook on the web and select the Settings gear.
2. Go to Calendar, then Shared calendars.
3. Under "Publish a calendar", pick the main calendar and the permission "Can view all details".
4. Select Publish. Two links appear: HTML and ICS.
5. Copy the ICS link and put it in Tether's environment file as a secret. Do not commit it.

If the publish section is missing or disabled, the organization has turned publishing off. The hub then runs on the Google sources only.

### Measured feed (first real feed, 2026-11-15)

What one published Exchange Online calendar actually returned. Build and test the ICS connector against these facts:

- One `GET` returns about 2.5 MB of `text/calendar`, with no ETag, Last-Modified or Cache-Control. Change detection therefore relies on the content hash alone.
- The window runs from about one year back to about two months ahead. Recurring masters carry their rules, so expansion still reaches 90 days ahead.
- There are 1,180 VEVENTs across 579 UIDs. There are 73 RRULE masters (63 weekly, 6 monthly, 3 daily, 1 yearly), 602 RECURRENCE-ID overrides and 35 EXDATEs. Most overrides share a UID with a master in the feed, so expansion must merge them into the series rather than treat them as separate events.
- All TZIDs are Windows names, each with a VTIMEZONE block: Eastern, Central, Pacific, India, Pakistan and GTB Standard Time. 204 events are all-day (`VALUE=DATE`). There are no UTC or floating date-times.
- Every event has `STATUS:CONFIRMED`. Free and busy status is only in `X-MICROSOFT-CDO-BUSYSTATUS`: 251 BUSY, 497 TENTATIVE, 416 FREE and 16 OOF. Unanswered invites arrive as TENTATIVE, so this field decides what counts as a real meeting.
- There are no ATTENDEE or ORGANIZER lines.
- It was published with all details. 1,129 events carry a DESCRIPTION, median about 1.5 KB, most with Teams join links.

## MCP tool surface

Seventeen tools, each a one-to-one wrapper over a service function that REST also calls. Nothing sends or replies. The only tool that deletes anything is `release_calendar_hold`, and it deletes only hub events that Tether itself created.

| Tool | Does |
| --- | --- |
| `get_overview()` | Returns the home screen as data: urgent items, section counts, waiting-on, today's agenda. This is the "what am I forgetting" call. |
| `list_items(account?, state?, limit?)` | Lists inbox items, newest first |
| `archive_item(id)` | Archives an item, writing through to Gmail where applicable |
| `unarchive_item(id)` | Reopens an item, adding INBOX back in Gmail where applicable |
| `list_tasks(status?, parent_id?)` | Lists tasks |
| `search_mail(query, account?, limit?)` | Searches one or all Gmail accounts with Gmail search syntax. Returns thread summaries |
| `get_thread(account, thread_id)` | Returns a thread's messages as text, fetched live and not stored |
| `create_task(title, parent_id?, item_id?, thread?, due_at?, priority?)` | Creates a task, optionally linked to an item or to a thread from a search |
| `update_task(id, fields)` | Edits title, notes, due date, priority or order |
| `complete_task(id)` | Completes a task and applies the archive rule |
| `list_follow_ups(status?)` | Lists follow-ups |
| `create_follow_up(item_id, waiting_for?, due_at?)` | Pins a manual follow-up |
| `resolve_follow_up(id, outcome)` | Confirms a proposed follow-up, or dismisses one |
| `ingest_digest(raw_text, digest_date?)` | Parses and upserts a work digest |
| `get_agenda(from, to)` | Returns hub events for a date range |
| `sync_status()` | Returns per-source health and last success |
| `release_calendar_hold(source_id, action)` | Applies or discards a held calendar delete (see Deletes) |

### MCP auth

The MCP endpoint is never an unauthenticated URL. Claude's connector cannot use the web app's session cookie, so MCP has its own auth.

- MCP is served over streamable HTTP by the same process.
- Auth follows the MCP authorization spec. Tether is the OAuth authorization server: protected-resource and authorization-server metadata, PKCE, and the client registration Claude's connector requires.
- The authorize step is Google sign-in, restricted to the owner's address (`TETHER_OWNER_EMAIL`), followed by a consent page naming the client and where it will return. Tether then issues its own access and refresh tokens bound to that identity. Google tokens never leave the server.
- Built on the MCP Python SDK's authorization-server routes: `/.well-known/oauth-protected-resource/mcp`, `/.well-known/oauth-authorization-server`, `/register` (dynamic client registration), `/authorize`, `/token` and `/revoke`. Tether supplies the storage and the consent step.
- Registered redirect URIs are limited to `TETHER_MCP_REDIRECT_HOSTS`: Claude's hosts over HTTPS, plus loopback for Claude Code. A stranger cannot register a client that sends codes elsewhere.
- Tokens are bound to the resource `<TETHER_BASE_URL>/mcp` and stored only as SHA-256 hashes. Access tokens last one hour. Refresh tokens last 30 days, sliding, and rotate on every use, so a used refresh token is refused. Codes are single-use and last five minutes. Revoking any token revokes its whole grant.
- Transport is stateless streamable HTTP with JSON responses, so a restart drops no sessions. Host-header checks accept only the base URL's host.
- This is a named work item at the start of phase 4. The flow is proven end to end against Claude's connector, using one real read-only tool (`sync_status`), before the rest of the tool surface is built on it.

## Mail search and reading

These two tools let Claude build the to-do list from every inbox, not only from what is open in Tether.

### search\_mail

- The query is passed to Gmail as written, so any Gmail search syntax works (`is:unread newer_than:3d`, `from:ann`, `has:attachment`).
- With no account, it runs against every Gmail account concurrently and merges the results, newest first.
- The category filter does not apply: an explicit search sees the whole mailbox. Claude can add `-category:promotions` itself.
- Each result carries account, thread ID, subject, sender, date, snippet, whether the thread is in the inbox, and the Tether `item_id` when the thread is already an item.
- `limit` defaults to 20 per account and caps at 50. A broad search returns summaries; Claude reads the threads it needs with `get_thread`.

### get\_thread

- Returns each non-draft message's sender, recipients, date and body as plain text. The text/plain part is preferred; HTML is converted to text. Attachments are listed by name and size, never fetched.
- Each message body is capped (default 20,000 characters), and the result says when it was truncated.
- Nothing from the response is stored or logged. The item's snippet and metadata update only through the normal sync.

### Tasks from mail

- `create_task` takes either an `item_id` or a `thread` (`{account, thread_id}`) from a search result.
- A thread reference upserts the item first, with its state mirrored from Gmail, then links the task. That puts the thread under sync, so a reply reopens it, and completing the task archives it, the same as any linked item.
- Claude creates tasks when you ask, or under a standing instruction you gave it. Both count as your consent; tasks are created directly, with no proposed state.

### Other sources

- Digest-only mail cannot be searched; Tether never has access to it.
- Slack and GroupMe are post-MVP. When Slack lands, its search (`search.messages`, scope `search:read`) follows the same pattern. GroupMe has no search API.

## Home screen: what counts as urgent

Urgent is a fixed rule list, not a model score, so you can always tell why something is there.

In display order:

1. Source alerts: an auth failure, a source stale past two hours, or a held calendar delete.
2. Tasks overdue or due today.
3. Follow-ups past due.
4. Open items with high priority, or due today or earlier.

Only digest items carry a priority or due date, so rule 4 applies to them alone in v1. A Gmail or Slack item reaches urgent only once you turn it into a task with a date.

Below the urgent strip sits one preview card per section, each linking out: Inbox, Tasks, Waiting on, Today's calendar. Every item row carries its account color.

## Runtime, hosting and security

In production, one FastAPI process on one server runs everything: the API, the MCP endpoint, the pollers as asyncio tasks, and, post-MVP, the Slack socket client.

### Runtime

- The pollers (and later the Slack socket) need an always-on process, which rules out serverless hosting.
- **Hard rule: exactly one process with exactly one worker.** The pollers and the Slack socket live inside it, so a second worker would duplicate every poller and socket. `python -m tether serve` starts uvicorn with exactly one worker and has no option for more; the systemd unit runs that command.
- The process runs under systemd. No Docker and no broker. The server may host other apps, so Tether runs as its own system user, on the port in `TETHER_PORT`, behind the reverse proxy.
- All site-specific values come from the environment: `TETHER_BASE_URL`, `TETHER_HOST`, `TETHER_PORT`, the owner identity and the secrets. The repo holds templates and `.env.example` files only.
- The React build is served as static files by the same process. The PWA manifest and service worker ship in phase 2. The service worker caches only the app shell, never API responses.
- SQLite in WAL mode. One writer process and one user make this sufficient. Backups: see Decisions.
- Move to Postgres only if a second process ever needs to write.

### Security

This server holds live tokens to every connected mailbox, so it is the thing to protect.

- Credentials live in the `credential` table, one row per account, encrypted at rest with a key from the environment, never from the database. The database file and the environment file are readable only by the Tether user, so other apps on the server cannot read them.
- Login is single-user: Google sign-in restricted to the owner's address, `TETHER_OWNER_EMAIL` (verified email, pinned to its Google account ID), reusing the OAuth app the Gmail connector already needs. HTTPS only.
- The ICS URL is stored as a secret in the environment file. Anyone holding it can read the published calendar.
- Logs never contain snippets, digest text or tokens.
- The server makes no LLM calls in v1. If the model pass over Sent is approved, it becomes the only one, and nothing from the digest goes through it. That covers the server only. Two paths send content to Claude: the digest passes through Claude on the way in, because Claude relays the pasted text to `ingest_digest`; and `search_mail` and `get_thread` return mail from the connected Google accounts to Claude when it calls them.
- Logs never contain message bodies or search queries.

## Build order

Six MVP phases, each ending in something you use daily. The home screen grows with them: every phase adds its card and its urgent rule.

1. **Confirm the starting point.** Done: the repo was greenfield.
2. **Vertical slice.** Schema, service layer, login, one Gmail account (initial-sync window and category filter included), inbox list in the PWA.
3. **All Gmail accounts.** Two-way archive, unarchive, and account colors.
4. **MCP auth, then tasks, MCP tools and the digest.** First prove the OAuth flow against Claude's connector with a stub tool. Then two-level tasks with item links, the MCP tools including `search_mail` and `get_thread`, and `ingest_digest`.
5. **Calendar hub.** Google sources first, then ICS feeds. Confirm publishing is enabled before starting, and measure the feed lag.
6. **Follow-ups.** Manual pin and auto-resolve. The model pass only once approved.

Post-MVP: Slack, then GroupMe.

Phase 5 depends on nothing in phases 3 and 4, so the build agent can run it in parallel.

Deferred: reply, the Android widget, Teams, Azure DevOps, Jira, and the model pass over Sent until approved.

## Changes in v1.1

From the design review:

- Every account grants `calendar.events` at connect (hub writes stay on the hub account). Calendar sync tokens are dropped for full-window fetch and diff.
- Hub assignment is sticky, with rules for splits, merges, new-group keys and 409 re-adoption.
- MCP gets its own OAuth flow, proven first in phase 4.
- Gmail category filter, sent-only thread rule, initial-sync window.
- Held calendar deletes get `missing_since`, a release action, and a 25%-and-5 threshold.
- Digest dedup hashes the parsed timestamp. ICS parsing named with a Windows TZ map.
- `credential` table replaces `token_ref`. `created_at`/`updated_at` on items and tasks, `created_at` on hub events, `last_evaluated_sent_id` on items. New tool `unarchive_item`.
- Release action for held calendar deletes is an MCP tool, `release_calendar_hold`, as well as a Settings action.
- Claude can search and read Gmail through MCP: `search_mail`, `get_thread`, and `create_task` taking a thread from a search. Bodies are fetched live and never stored.
- A Claude tool call counts as your consent, including under a standing instruction; no per-request ask and no proposed state on tasks.
- MVP scope is email, calendar and tasks. Slack and GroupMe are post-MVP.
- GroupMe follow-ups are manual-resolve only. One-worker hard rule. Nightly backup removed.
- Sync window and category filter are editable per account in Settings; a change triggers a full reconcile.
- Gmail quota: requests are paced under the per-user limit, quota refusals are retried rather than treated as auth failures, and the first sync is resumable.
- Decisions recorded: hosting, publish level, model pass not approved, no `WAITING` block, digest data storage, backups out of scope, the LLM line corrected.

## Changes in v1.2

- The repo describes a generic tool. Deployment values (public URL, port, deploy target) come from environment files; the repo carries templates and `.env.example` files only.
- `python -m tether serve` is the one entry point and always runs one worker.
- MCP auth as built: SDK authorization-server routes, owner consent page, redirect-host allowlist, hashed and rotating tokens.
- The manual mail account kind is `digest`. The digest's time zone is configurable (`TETHER_TIMEZONE`, built with the digest in phase 4).
