-- Tether schema v1 (spec v1.1, "Data model"). Nine tables, single user.
-- Timestamps are ISO 8601 UTC text ("2026-11-15T13:04:05Z").

CREATE TABLE account (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('gmail', 'slack', 'groupme', 'digest', 'ics')),
    label               TEXT NOT NULL UNIQUE,
    color               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'auth_error', 'disabled')),
    -- Gmail only: comma-separated category names excluded from the inbox.
    excluded_categories TEXT
);

CREATE TABLE credential (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL UNIQUE REFERENCES account(id),
    ciphertext  BLOB NOT NULL,
    scopes      TEXT NOT NULL DEFAULT '',
    expires_at  TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE item (
    id                      INTEGER PRIMARY KEY,
    account_id              INTEGER NOT NULL REFERENCES account(id),
    external_id             TEXT NOT NULL,
    kind                    TEXT NOT NULL CHECK (kind IN ('email', 'chat', 'digest')),
    title                   TEXT NOT NULL DEFAULT '',
    sender                  TEXT NOT NULL DEFAULT '',
    snippet                 TEXT NOT NULL DEFAULT '',
    needed                  TEXT,
    url                     TEXT,
    last_activity_at        TEXT NOT NULL,
    last_direction          TEXT NOT NULL CHECK (last_direction IN ('in', 'out')),
    state                   TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'archived')),
    archived_through        TEXT,
    priority                TEXT CHECK (priority IN ('high', 'normal', 'low')),
    due_at                  TEXT,
    last_evaluated_sent_id  TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE (account_id, external_id)
);
CREATE INDEX item_state_activity ON item (state, last_activity_at DESC);

CREATE TABLE task (
    id            INTEGER PRIMARY KEY,
    parent_id     INTEGER REFERENCES task(id),
    title         TEXT NOT NULL,
    notes         TEXT,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    due_at        TEXT,
    priority      TEXT CHECK (priority IN ('high', 'normal', 'low')),
    sort_order    REAL NOT NULL DEFAULT 0,
    item_id       INTEGER REFERENCES item(id),
    completed_at  TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE follow_up (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES item(id),
    waiting_for  TEXT,
    sent_at      TEXT,
    due_at       TEXT,
    status       TEXT NOT NULL CHECK (status IN ('proposed', 'waiting', 'replied', 'dismissed')),
    source       TEXT NOT NULL CHECK (source IN ('manual', 'classifier', 'digest'))
);
-- One non-terminal follow-up per item.
CREATE UNIQUE INDEX follow_up_one_open ON follow_up (item_id) WHERE status IN ('proposed', 'waiting');

CREATE TABLE calendar_source (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES account(id),
    kind          TEXT NOT NULL CHECK (kind IN ('google', 'ics')),
    external_ref  TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (account_id, external_ref)
);

CREATE TABLE hub_event (
    id               INTEGER PRIMARY KEY,
    dedup_key        TEXT NOT NULL UNIQUE,
    google_event_id  TEXT NOT NULL,
    content_hash     TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE source_event (
    id              INTEGER PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES calendar_source(id),
    uid             TEXT NOT NULL,
    instance_start  TEXT NOT NULL,
    start           TEXT NOT NULL,
    "end"           TEXT NOT NULL,
    all_day         INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    location        TEXT,
    description     TEXT,
    modified_at     TEXT,
    content_hash    TEXT,
    hub_event_id    INTEGER REFERENCES hub_event(id),
    missing_since   TEXT,
    UNIQUE (source_id, uid, instance_start)
);

CREATE TABLE sync_state (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES account(id),
    stream           TEXT NOT NULL,
    cursor           TEXT,
    last_attempt_at  TEXT,
    last_ok_at       TEXT,
    failures         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    UNIQUE (account_id, stream)
);
