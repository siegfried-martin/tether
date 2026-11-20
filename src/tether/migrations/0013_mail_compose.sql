-- Drafts Claude made through Tether (only these can be sent from Tether), and a log of every send.
CREATE TABLE mail_draft (
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    draft_id    TEXT NOT NULL,
    thread_id   TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (account_id, draft_id)
);

-- One row per message sent. Recipients are JSON lists of addresses as sent.
CREATE TABLE mail_send_log (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL,
    account     TEXT NOT NULL,
    draft_id    TEXT NOT NULL,
    message_id  TEXT,
    thread_id   TEXT,
    to_addrs    TEXT NOT NULL,
    cc_addrs    TEXT NOT NULL,
    bcc_addrs   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    sent_at     TEXT NOT NULL
);
CREATE INDEX mail_send_log_sent_at ON mail_send_log (sent_at);
