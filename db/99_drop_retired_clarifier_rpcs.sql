-- ============================================================
-- Migration 99: Drop retired clarifier RPCs (plans/73)
--
-- The graph clarification question flow is retired (db/98 + code). These
-- DB objects were only ever called by the deleted clarifier machinery and
-- have no remaining callers:
--
--   - next_clarification_shortcode()      — issued c{shortcode} for questions
--   - clarification_seq                   — backing sequence for the above
--   - cleanup_expired_clarifications()    — never called anywhere (verified)
--
-- pending_graph_clarifications (NLP correction session state) is a SEPARATE,
-- live feature and is untouched.
-- ============================================================

DROP FUNCTION IF EXISTS public.next_clarification_shortcode();
DROP SEQUENCE IF EXISTS public.clarification_seq;

-- Defined twice over history (db/37 no-arg, db/80 with owner_id) — drop both
-- signatures so no overload survives.
DROP FUNCTION IF EXISTS public.cleanup_expired_clarifications();
DROP FUNCTION IF EXISTS public.cleanup_expired_clarifications(uuid);

-- Verify: each should return 0 rows
SELECT 'next_clarification_shortcode' AS obj, count(*) AS remaining
FROM pg_proc WHERE proname = 'next_clarification_shortcode';
SELECT 'clarification_seq' AS obj, count(*) AS remaining
FROM pg_class WHERE relname = 'clarification_seq';
SELECT 'cleanup_expired_clarifications' AS obj, count(*) AS remaining
FROM pg_proc WHERE proname = 'cleanup_expired_clarifications';
