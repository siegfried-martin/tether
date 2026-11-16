-- What Tether needs to delete an event from Google (from the event panel):
-- external_id: Google's event ID for this instance (for a repeating event, the one occurrence);
-- series_id: Google's ID of the repeating series it belongs to, if any;
-- organizer_self: the calendar's owner organizes it (not just a guest); guests: how many other people
-- are invited. ICS events leave them empty. They fill in on each source's next fetch.
ALTER TABLE source_event ADD COLUMN external_id TEXT;
ALTER TABLE source_event ADD COLUMN series_id TEXT;
ALTER TABLE source_event ADD COLUMN organizer_self INTEGER NOT NULL DEFAULT 1;
ALTER TABLE source_event ADD COLUMN guests INTEGER NOT NULL DEFAULT 0;
