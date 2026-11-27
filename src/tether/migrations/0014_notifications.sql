-- Notifications (Web Push) for new mail and event reminders.

-- Minutes before start that the event's own notifications fire, comma-separated ("10,30").
-- '' means the event says no notifications; NULL means it doesn't say (Tether then uses 30).
ALTER TABLE source_event ADD COLUMN reminders TEXT;
-- A Google calendar's default notifications, the same way; used by events that keep the defaults.
ALTER TABLE calendar_source ADD COLUMN default_reminders TEXT;

-- One row per browser or app install that asked for notifications. endpoint is the push
-- service's URL for that device; p256dh and auth are its keys for encrypting what is sent.
CREATE TABLE push_subscription (
    id          INTEGER PRIMARY KEY,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    mail        INTEGER NOT NULL DEFAULT 1,
    events      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    last_ok_at  TEXT,
    failures    INTEGER NOT NULL DEFAULT 0
);

-- What has been notified, so nothing is sent twice: "mail:<item>:<activity>", "event:<hub>:<start>:<minutes>".
CREATE TABLE notification_log (
    key      TEXT PRIMARY KEY,
    sent_at  TEXT NOT NULL
);
