-- db/21_case_insensitive_label.sql
-- Replace the case-sensitive unique_label constraint with a case-insensitive index
-- to prevent case-collided duplicate graph nodes from being created.

ALTER TABLE graph_nodes DROP CONSTRAINT IF EXISTS unique_label;
CREATE UNIQUE INDEX unique_label_lower ON graph_nodes (LOWER(TRIM(label)));