-- Migration 35: Drop pending_graph_nodes
-- The old pending_graph_nodes table has been replaced by:
--   - pending_nodes (node creation approvals)
--   - merge_proposals (merge target→source proposals)
--
-- Prerequisites:
--   1. db/34 migration applied (tables created)
--   2. Backfill script executed (381 rows migrated)
--   3. All code readers swapped to pending_nodes
--   4. Dual-write code removed from Python codebase
--
-- CASCADE will drop any FK references (none should exist, but CASCADE is safe).

DROP TABLE IF EXISTS public.pending_graph_nodes CASCADE;

-- Verify: the new tables should have all the data
SELECT 'pending_nodes count: ' || COUNT(*)::TEXT FROM public.pending_nodes;
SELECT 'merge_proposals count: ' || COUNT(*)::TEXT FROM public.merge_proposals;

-- Comment for documentation
COMMENT ON TABLE public.pending_nodes IS 'Node creation approvals (replaces pending_graph_nodes).';
COMMENT ON TABLE public.merge_proposals IS 'Merge target→source proposals (replaces merge_proposed status in pending_graph_nodes).';
