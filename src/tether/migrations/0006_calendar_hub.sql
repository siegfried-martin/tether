-- Calendar hub (phase 5).
-- calendar_source.name: the calendar's display name (Google summary, or the ICS label).
ALTER TABLE calendar_source ADD COLUMN name TEXT NOT NULL DEFAULT '';

-- source_event.busy: busy, tentative, free or oof. Free events mark the hub copy as "show as free".
ALTER TABLE source_event ADD COLUMN busy TEXT NOT NULL DEFAULT 'busy';
CREATE INDEX source_event_start ON source_event (start);
CREATE INDEX source_event_hub ON source_event (hub_event_id);

-- hub_event keeps the merged content, so the agenda is served without asking Google.
-- content_hash (from 0001) is the hash last written to Google; NULL means not yet written.
ALTER TABLE hub_event ADD COLUMN start TEXT;
ALTER TABLE hub_event ADD COLUMN "end" TEXT;
ALTER TABLE hub_event ADD COLUMN all_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hub_event ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE hub_event ADD COLUMN location TEXT;
ALTER TABLE hub_event ADD COLUMN description TEXT;
ALTER TABLE hub_event ADD COLUMN busy TEXT NOT NULL DEFAULT 'busy';
-- Hash of the merged content above; differs from content_hash while a write is pending.
ALTER TABLE hub_event ADD COLUMN desired_hash TEXT;
CREATE INDEX hub_event_start ON hub_event (start);
