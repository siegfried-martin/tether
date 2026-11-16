-- calendar_source.access_role: the account's access to that calendar, from Google's calendar list
-- (owner, writer, reader, freeBusyReader), so new events are offered only where they can be written.
-- is_primary marks the account's own main calendar, the default for a new event.
-- Both fill in on the next calendar list refresh, which runs when the server starts.
ALTER TABLE calendar_source ADD COLUMN access_role TEXT;
ALTER TABLE calendar_source ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0;
