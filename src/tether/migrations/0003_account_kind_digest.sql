-- migrate: rebuild
-- Rebuild `account` so its kind CHECK matches 0001: the manual digest account kind is 'digest'.
-- On a database created from the current 0001 this is a no-op copy.

CREATE TABLE account_new (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('gmail', 'slack', 'groupme', 'digest', 'ics')),
    label               TEXT NOT NULL UNIQUE,
    color               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'auth_error', 'disabled')),
    -- Gmail only: comma-separated category names excluded from the inbox.
    excluded_categories TEXT,
    -- Gmail only: initial-sync window in days; NULL means the config default.
    initial_sync_days   INTEGER CHECK (initial_sync_days BETWEEN 1 AND 90)
);

INSERT INTO account_new (id, kind, label, color, status, excluded_categories, initial_sync_days)
SELECT id, CASE WHEN kind IN ('gmail', 'slack', 'groupme', 'ics') THEN kind ELSE 'digest' END,
       label, color, status, excluded_categories, initial_sync_days
FROM account;

DROP TABLE account;
ALTER TABLE account_new RENAME TO account;
