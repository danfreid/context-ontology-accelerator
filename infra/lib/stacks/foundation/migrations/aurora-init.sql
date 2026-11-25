-- Aurora PostgreSQL 16 initialisation for the switchable-backends Aurora path
-- (graph_backend=aurora, vector_backend=pgvector, metadata_backend=aurora).
--
-- Run once against the cluster after creation (RDS Data API or psql). Not
-- synthesised into CloudFormation.

-- ── Extensions ─────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector: embedding similarity search
CREATE EXTENSION IF NOT EXISTS pg_search;  -- ParadeDB: BM25 full-text; combine with pgvector via reciprocal rank fusion
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
