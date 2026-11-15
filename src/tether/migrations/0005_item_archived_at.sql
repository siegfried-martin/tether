-- When an item last moved to archived, for the "recently archived" view. NULL for items
-- that were never open (e.g. sent-only threads) and for open items.
ALTER TABLE item ADD COLUMN archived_at TEXT;
CREATE INDEX item_archived_at ON item (archived_at DESC) WHERE state = 'archived';
