-- The Tasks page now follows sort_order (drag to reorder) instead of creation time.
-- Renumber each set of siblings 1..n in creation order, so the page looks the same as before.
UPDATE task SET sort_order = (
    SELECT COUNT(*) FROM task s
    WHERE s.parent_id IS task.parent_id
      AND (s.created_at < task.created_at OR (s.created_at = task.created_at AND s.id <= task.id))
);
