-- Task tags: the owner's own labels for tasks ("Zion", "Home", "Waiting on"), any number per task.
-- Names are unique ignoring case. color is a hex color, picked from the account palette.
CREATE TABLE tag (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE task_tag (
    task_id  INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);
CREATE INDEX task_tag_tag ON task_tag (tag_id);
