-- Ground-truth labels for retrieval evaluation.
-- Seed with 15-20 hand-labeled examples covering entity lookup,
-- topic query, temporal, and ambiguous categories.
-- Directional measurement only — not enough for claiming final quality.

CREATE TABLE IF NOT EXISTS public.retrieval_eval_gold (
    id                BIGSERIAL PRIMARY KEY,
    query_text        TEXT NOT NULL,
    expected_memory_ids JSONB NOT NULL DEFAULT '[]',  -- array of memory IDs
    category          TEXT NOT NULL DEFAULT 'general'
                        CHECK (category IN ('entity_lookup','topic_query','temporal','ambiguous','general')),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_gold_query ON public.retrieval_eval_gold(query_text);

-- Extend eval_results with per-query metric columns (idempotent)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'retrieval_eval_results'
    ) THEN
        ALTER TABLE public.retrieval_eval_results
            ADD COLUMN IF NOT EXISTS recall_at_5   REAL,
            ADD COLUMN IF NOT EXISTS recall_at_8   REAL,
            ADD COLUMN IF NOT EXISTS recall_at_12  REAL,
            ADD COLUMN IF NOT EXISTS precision_at_5  REAL,
            ADD COLUMN IF NOT EXISTS precision_at_8  REAL,
            ADD COLUMN IF NOT EXISTS precision_at_12 REAL,
            ADD COLUMN IF NOT EXISTS expected_count INT;
    END IF;
END
$$;
