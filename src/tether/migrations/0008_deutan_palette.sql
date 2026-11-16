-- Replace the Okabe-Ito account colors with a palette chosen for deuteranomaly. Okabe-Ito
-- holds up as solid marks, but as pale row tints its orange and gold (and its pink and teal)
-- become indistinguishable. These five stay apart as tints, light and dark, under simulated
-- deuteranomaly. Slot for slot; colors the owner picked are left alone. The work feed moves
-- to amber (it was gold) and the account that was orange becomes rust.
UPDATE account SET color = CASE color
    WHEN '#0072b2' THEN '#4377f0'
    WHEN '#e69f00' THEN '#b23b19'
    WHEN '#cc79a7' THEN '#b0267c'
    WHEN '#009e73' THEN '#48cba1'
    WHEN '#d4b000' THEN '#ee9e10'
    WHEN '#56b4e9' THEN '#21c5e5'
    WHEN '#d55e00' THEN '#2e7a50'
    ELSE color END;
