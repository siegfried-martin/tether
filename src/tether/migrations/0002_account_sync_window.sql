-- Per-account import window (days). NULL means the configured default.
ALTER TABLE account ADD COLUMN initial_sync_days INTEGER CHECK (initial_sync_days BETWEEN 1 AND 90);
