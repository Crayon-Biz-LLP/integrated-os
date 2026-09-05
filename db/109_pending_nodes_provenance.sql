-- 109_pending_nodes_provenance.sql
--
-- Root Cause: The evidence-gate hardening (P2) stamps every pending row with
-- provenance {origin_table, origin_id, ...} so untraceable-ghost rows (the
-- "Please" node family, e.g. node 1009) can always be traced to the
-- message/raw_dump/run that created them. The writer
-- (core/lib/entity_context.py _create_pending_org/_create_pending_person)
-- emits pending_nodes.provenance, but the column was never created — any
-- decision-gated queue call that passes provenance fails the INSERT with
-- 42703 (unknown column), the exception is swallowed by the writer's
-- try/except, and the pending row is silently dropped.
--
-- Fix: add the column. text (not jsonb) because the writer stores
-- json.dumps(...) — a serialized string; a jsonb column would double-encode
-- it into a JSON string value.
--
-- Pattern: additive and idempotent (ADD COLUMN IF NOT EXISTS) so apply is
-- safe on every environment, mirroring the db/108 apply approach.

ALTER TABLE public.pending_nodes
    ADD COLUMN IF NOT EXISTS provenance text;

COMMENT ON COLUMN public.pending_nodes.provenance IS
    'JSON {origin_table, origin_id, ...}: the message/raw_dump/run that created this pending row (evidence-gate hardening P2).';
