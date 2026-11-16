-- Recolor accounts still on the old default palette with the color-blind-safe one,
-- slot for slot. Colors the owner picked are left alone.
UPDATE account SET color = CASE color
    WHEN '#2f6fdb' THEN '#0072b2'
    WHEN '#d9480f' THEN '#e69f00'
    WHEN '#2b8a3e' THEN '#cc79a7'
    WHEN '#ae3ec9' THEN '#009e73'
    WHEN '#e8590c' THEN '#d4b000'
    WHEN '#0c8599' THEN '#56b4e9'
    WHEN '#c2255c' THEN '#d55e00'
    WHEN '#5f3dc4' THEN '#6b7280'
    ELSE color END;
-- The work digest takes the work calendar feed's color, so work reads as one thing.
UPDATE account SET color = (SELECT color FROM account WHERE kind = 'ics' ORDER BY id LIMIT 1)
WHERE kind = 'digest' AND EXISTS (SELECT 1 FROM account WHERE kind = 'ics');
