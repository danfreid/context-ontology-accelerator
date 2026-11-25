-- Aurora PostgreSQL 18 initialisation for the switchable-backends Aurora path
-- (graph_backend=aurora, vector_backend=pgvector, metadata_backend=aurora).
--
-- Run once against the cluster after creation (RDS Data API or psql). Not
-- synthesised into CloudFormation.

-- ── Extensions ─────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector: embedding similarity search
-- Keyword search uses built-in tsvector + ts_rank (GIN index); merge with pgvector
-- results by reciprocal rank fusion in SQL. ParadeDB pg_search is not supported on Aurora.
CREATE EXTENSION IF NOT EXISTS pgrouting;  -- shortest-path / Dijkstra graph traversal

-- ── DynamoDB replacement tables ────────────────────────────────────────────
-- TODO: mirror DynamoDB table schema here. Derive it from the single-table
-- (PK/SK) design in stacks/services/ontology-stack.ts and
-- stacks/foundation/authnz-stack.ts in a later phase.
--
-- Placeholder shape for the single-table design:
CREATE TABLE IF NOT EXISTS ontology_engine (
    pk         TEXT  NOT NULL,
    sk         TEXT  NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (pk, sk)
);

-- ── Graph traversal notes (recursive CTEs) ─────────────────────────────────
-- Neptune follows edges by index-free adjacency; Postgres does a B-tree lookup
-- per hop, so traversal queries need these precautions.
--
-- 1. Covering index on the edge table, so each hop is an index-only scan with
--    no heap lookup. INCLUDE any edge property used in a filter:
--
--      CREATE INDEX IF NOT EXISTS graph_edge_source_target_idx
--          ON graph_edge (source_id, target_id) INCLUDE (edge_type, weight);
--
-- 2. Prune early: put filters (edge_type, depth limit) inside the recursive
--    term, so dead-end paths are not expanded.
--
-- 3. Track visited nodes: carry a path array and reject nodes already on it,
--    which prevents cycles and repeated work:
--
--      WITH RECURSIVE walk(node_id, path, depth) AS (
--          SELECT e.target_id, ARRAY[e.source_id, e.target_id], 1
--          FROM graph_edge e
--          WHERE e.source_id = $1
--        UNION            -- UNION, not UNION ALL: deduplicates intermediate
--                         -- rows when only reachability matters
--          SELECT e.target_id, w.path || e.target_id, w.depth + 1
--          FROM walk w
--          JOIN graph_edge e ON e.source_id = w.node_id
--          WHERE NOT e.target_id = ANY(w.path)   -- visited-node pruning
--            AND w.depth < $2                    -- early depth cut-off
--      )
--      SELECT DISTINCT node_id FROM walk;
--
-- 4. Hierarchical-only data (taxonomies, org trees): consider the ltree
--    extension with a GiST index instead of a recursive join.
--
-- TODO: pgRouting handles shortest-path/Dijkstra queries (pgr_dijkstra, pgr_aStar).
-- For 4+ hop traversals on 500M+ nodes Neptune retains a performance advantage:
-- benchmark deep traversal queries (tests/benchmark) before committing to an
-- aurora-only graph backend. Apache AGE (openCypher) is the next option if
-- hand-written CTEs fall short.

-- ── Lexical + hybrid search (replaces AOSS keyword search) ─────────────────
-- Aurora has no BM25 extension (no pg_search / pg_textsearch). Built-in ts_rank
-- ignores corpus statistics, so ranking is done in two stages:
--   1. Recall: tsvector + GIN pulls the top ~200 candidates per query.
--   2. Rank: the caller computes true BM25 over those candidates from the
--      term statistics below (k1 = 1.2, b = 0.75), then fuses with the pgvector
--      ranking by reciprocal rank fusion (score = sum 1 / (60 + rank)).
-- Phase 2 application code owns steps 2 and the statistics upkeep.
CREATE TABLE IF NOT EXISTS search_doc (
    doc_id     TEXT PRIMARY KEY,
    namespace  TEXT NOT NULL,
    body       TEXT NOT NULL,
    doc_len    INTEGER NOT NULL,                       -- token count, for BM25 length normalisation
    tsv        TSVECTOR NOT NULL,
    embedding  VECTOR                                  -- dimension fixed per index at runtime
);
CREATE INDEX IF NOT EXISTS search_doc_tsv_idx ON search_doc USING GIN (tsv);
CREATE INDEX IF NOT EXISTS search_doc_namespace_idx ON search_doc (namespace);

-- Per-namespace corpus statistics: document count and average length.
CREATE TABLE IF NOT EXISTS search_corpus_stats (
    namespace  TEXT PRIMARY KEY,
    doc_count  BIGINT NOT NULL DEFAULT 0,
    avg_doc_len DOUBLE PRECISION NOT NULL DEFAULT 0
);

-- Per-namespace document frequency per term, for IDF.
CREATE TABLE IF NOT EXISTS search_term_stats (
    namespace  TEXT NOT NULL,
    term       TEXT NOT NULL,
    doc_freq   BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, term)
);
