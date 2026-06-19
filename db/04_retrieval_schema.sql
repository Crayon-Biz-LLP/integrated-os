-- HippoRAG-style Derived Retrieval Schema
-- Additive: all tables use retrieval_ prefix, no changes to canonical tables.
-- Deploy-safe: creates new infrastructure only.

-- ============================================================
-- 1. Index Runs — tracks indexing state per source item
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_index_runs (
    id              BIGSERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL,              -- 'memory', 'task', 'raw_dump', 'message', 'email', etc.
    source_id       TEXT NOT NULL,              -- canonical pk as text
    source_fingerprint TEXT,                    -- content hash for idempotent skip
    index_version   INT NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','completed','failed')),
    error           TEXT,
    retry_count     INT NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, index_version)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_index_runs_status ON public.retrieval_index_runs(status);
CREATE INDEX IF NOT EXISTS idx_retrieval_index_runs_source ON public.retrieval_index_runs(source_type, source_id);

-- ============================================================
-- 2. Passages — chunked passages with embeddings
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_passages (
    id              BIGSERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    memory_id       BIGINT,                     -- FK to memories.id when applicable
    passage_index   INT NOT NULL,
    text            TEXT NOT NULL,
    char_count      INT NOT NULL DEFAULT 0,
    embedding       vector(768),
    source_fingerprint TEXT,
    index_version   INT NOT NULL DEFAULT 1,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_fingerprint, passage_index, index_version)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_passages_memory ON public.retrieval_passages(memory_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_passages_source ON public.retrieval_passages(source_type, source_id);

-- ============================================================
-- 3. Triples — subject-predicate-object extractions
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_triples (
    id                BIGSERIAL PRIMARY KEY,
    source_type       TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    passage_id        BIGINT REFERENCES public.retrieval_passages(id) ON DELETE CASCADE,
    subject_text      TEXT NOT NULL,
    predicate_text    TEXT NOT NULL,
    object_text       TEXT NOT NULL,
    normalized_subject  TEXT NOT NULL,
    normalized_predicate TEXT NOT NULL,
    normalized_object   TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0,
    extraction_model  TEXT,
    index_version     INT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (passage_id, normalized_subject, normalized_predicate, normalized_object, index_version)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_triples_passage ON public.retrieval_triples(passage_id);

-- ============================================================
-- 4. Phrase Nodes — normalized entities/concepts for graph propagation
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_phrase_nodes (
    id                BIGSERIAL PRIMARY KEY,
    normalized_text   TEXT NOT NULL UNIQUE,
    display_text      TEXT NOT NULL,
    node_type         TEXT DEFAULT 'concept'
                        CHECK (node_type IN ('entity','concept','person','project','organization','place','event','topic','phrase','emotional_state','animal','practice')),
    embedding         vector(768),
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata          JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_retrieval_phrase_nodes_type ON public.retrieval_phrase_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_retrieval_phrase_nodes_normalized ON public.retrieval_phrase_nodes(normalized_text);

-- ============================================================
-- 5. Edges — weighted edges between phrase nodes
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_edges (
    id                BIGSERIAL PRIMARY KEY,
    from_node_id      BIGINT NOT NULL REFERENCES public.retrieval_phrase_nodes(id) ON DELETE CASCADE,
    to_node_id        BIGINT NOT NULL REFERENCES public.retrieval_phrase_nodes(id) ON DELETE CASCADE,
    edge_type         TEXT NOT NULL DEFAULT 'related'
                        CHECK (edge_type IN ('related','co_occurs','subject_of','object_of','predicate_shared','alias','manual')),
    weight            REAL NOT NULL DEFAULT 1.0,
    source_triple_id  BIGINT REFERENCES public.retrieval_triples(id) ON DELETE SET NULL,
    source_passage_id BIGINT REFERENCES public.retrieval_passages(id) ON DELETE SET NULL,
    index_version     INT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_node_id, to_node_id, edge_type, index_version)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_edges_from ON public.retrieval_edges(from_node_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_edges_to ON public.retrieval_edges(to_node_id);

-- ============================================================
-- 6. Alias Edges — synonym/alias links between phrase nodes
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_alias_edges (
    id                BIGSERIAL PRIMARY KEY,
    from_node_id      BIGINT NOT NULL REFERENCES public.retrieval_phrase_nodes(id) ON DELETE CASCADE,
    to_node_id        BIGINT NOT NULL REFERENCES public.retrieval_phrase_nodes(id) ON DELETE CASCADE,
    alias_type        TEXT NOT NULL DEFAULT 'heuristic'
                        CHECK (alias_type IN ('canonicalization','embedding_similarity','heuristic','manual')),
    weight            REAL NOT NULL DEFAULT 0.8,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (from_node_id, to_node_id, alias_type)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_alias_from ON public.retrieval_alias_edges(from_node_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_alias_to ON public.retrieval_alias_edges(to_node_id);

-- ============================================================
-- 7. Passage↔Phrase Links
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_passage_phrase_links (
    id                BIGSERIAL PRIMARY KEY,
    passage_id        BIGINT NOT NULL REFERENCES public.retrieval_passages(id) ON DELETE CASCADE,
    node_id           BIGINT NOT NULL REFERENCES public.retrieval_phrase_nodes(id) ON DELETE CASCADE,
    role              TEXT NOT NULL DEFAULT 'mention'
                        CHECK (role IN ('subject','predicate','object','mention','topic')),
    weight            REAL NOT NULL DEFAULT 1.0,
    UNIQUE (passage_id, node_id, role)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_ppl_passage ON public.retrieval_passage_phrase_links(passage_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_ppl_node ON public.retrieval_passage_phrase_links(node_id);

-- ============================================================
-- 8. Passage↔Triple Links
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_passage_triple_links (
    id                BIGSERIAL PRIMARY KEY,
    passage_id        BIGINT NOT NULL REFERENCES public.retrieval_passages(id) ON DELETE CASCADE,
    triple_id         BIGINT NOT NULL REFERENCES public.retrieval_triples(id) ON DELETE CASCADE,
    UNIQUE (passage_id, triple_id)
);

-- ============================================================
-- 9. Memory Bundle Links — passages → canonical memories
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_memory_bundle_links (
    id                BIGSERIAL PRIMARY KEY,
    memory_id         BIGINT NOT NULL,          -- FK to memories.id (no explicit FK to avoid cross-schema dependency)
    passage_id        BIGINT NOT NULL REFERENCES public.retrieval_passages(id) ON DELETE CASCADE,
    index_version     INT NOT NULL DEFAULT 1,
    UNIQUE (memory_id, passage_id)
);

CREATE INDEX IF NOT EXISTS idx_retrieval_mbl_memory ON public.retrieval_memory_bundle_links(memory_id);

-- ============================================================
-- 10. Node Stats — DF/specificity for weighting
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_node_stats (
    id                BIGSERIAL PRIMARY KEY,
    node_id           BIGINT NOT NULL REFERENCES public.retrieval_phrase_nodes(id) ON DELETE CASCADE UNIQUE,
    df                INT NOT NULL DEFAULT 0,   -- document frequency (number of passages containing this node)
    source_count      INT NOT NULL DEFAULT 0,   -- number of source items
    specificity_score REAL NOT NULL DEFAULT 0.5,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 11. Eval Runs & Results
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_eval_runs (
    id                BIGSERIAL PRIMARY KEY,
    run_name          TEXT NOT NULL,
    run_type          TEXT NOT NULL DEFAULT 'shadow'
                        CHECK (run_type IN ('shadow','baseline','blended')),
    status            TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','completed','failed')),
    total_queries     INT NOT NULL DEFAULT 0,
    completed_queries INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.retrieval_eval_results (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT NOT NULL REFERENCES public.retrieval_eval_runs(id) ON DELETE CASCADE,
    query_text        TEXT NOT NULL,
    current_top_k     JSONB,                    -- IDs and scores from current retrieval
    associative_top_k JSONB,                    -- IDs and scores from new retrieval
    blended_top_k     JSONB,                    -- IDs and scores from blended
    current_latency_ms  INT,
    associative_latency_ms INT,
    blended_latency_ms   INT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- 12. Feature Flags (env-based, but store state for observability)
-- ============================================================
CREATE TABLE IF NOT EXISTS public.retrieval_config (
    key               TEXT PRIMARY KEY,
    value             TEXT NOT NULL,
    description       TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.retrieval_config (key, value, description) VALUES
    ('indexing_enabled', 'false', 'Master switch for retrieval indexing pipeline'),
    ('associative_enabled', 'false', 'Enable associative retrieval for query pipeline'),
    ('shadow_mode', 'false', 'Run new pipeline alongside current without switching'),
    ('briefing_enabled', 'false', 'Use associative retrieval in AI briefings'),
    ('debug_explanations', 'false', 'Include debug trace in query results')
ON CONFLICT (key) DO NOTHING;
