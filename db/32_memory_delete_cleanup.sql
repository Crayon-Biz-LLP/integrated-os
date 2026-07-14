-- db/32_memory_delete_cleanup.sql
-- AFTER DELETE trigger on memories table to guarantee real-time cleanup of
-- retrieval index entries (ghost vectors). Eliminates reliance on Python
-- cleanup_memory_retrieval_index() and sweep_orphan_retrieval_entries().

-- The cleanup function: deletes bundle links, passages (cascading to
-- passage_phrase_links and passage_triple_links via FK), and index runs.
CREATE OR REPLACE FUNCTION trg_cascade_memory_delete()
RETURNS TRIGGER AS $$
BEGIN
    -- 1. Remove bundle links by memory_id
    DELETE FROM public.retrieval_memory_bundle_links WHERE memory_id = OLD.id;

    -- 2. Remove passages (cascades to retrieval_passage_phrase_links
    --    and retrieval_passage_triple_links via their ON DELETE CASCADE FKs)
    DELETE FROM public.retrieval_passages WHERE source_type = 'memory' AND source_id = OLD.id::text;

    -- 3. Clean up index tracking rows
    DELETE FROM public.retrieval_index_runs WHERE source_type = 'memory' AND source_id = OLD.id::text;

    -- 4. Clean up phrase-node-level triples that reference this memory
    DELETE FROM public.retrieval_triples WHERE source_type = 'memory' AND source_id = OLD.id::text;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- Attach to the memories table
DROP TRIGGER IF EXISTS trg_memories_cleanup ON public.memories;
CREATE TRIGGER trg_memories_cleanup
AFTER DELETE ON public.memories
FOR EACH ROW
EXECUTE FUNCTION trg_cascade_memory_delete();
