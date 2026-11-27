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
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- trigram similarity: typo-tolerant entity-name matching
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch; -- levenshtein / soundex, for re-checking close trigram matches

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

CREATE INDEX IF NOT EXISTS ontology_engine_pk_prefix ON ontology_engine (pk text_pattern_ops);
-- Triple store for the Aurora graph backend (one row per RDF quad). Terms are stored
-- flat: o_kind is iri | bnode | literal. md5(o) keeps long literals out of the btree
-- key (about 2.7 KB limit) and still gives set semantics.
CREATE TABLE IF NOT EXISTS coa_triple (
    id          BIGSERIAL PRIMARY KEY,
    namespace   TEXT NOT NULL,
    graph_uri   TEXT NOT NULL,
    s           TEXT NOT NULL,
    s_kind      TEXT NOT NULL DEFAULT 'iri',
    p           TEXT NOT NULL,
    o           TEXT NOT NULL,
    o_kind      TEXT NOT NULL,
    o_lang      TEXT NOT NULL DEFAULT '',
    o_datatype  TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS coa_triple_uq
    ON coa_triple (graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype);
-- Traversal: covering indexes over IRI-to-IRI edges, namespace first because every read is namespace scoped.
CREATE INDEX IF NOT EXISTS coa_triple_fwd
    ON coa_triple (namespace, s, p) INCLUDE (o, graph_uri) WHERE o_kind = 'iri';
CREATE INDEX IF NOT EXISTS coa_triple_rev
    ON coa_triple (namespace, o, p) INCLUDE (s, graph_uri) WHERE o_kind = 'iri';
CREATE INDEX IF NOT EXISTS coa_triple_ns_p ON coa_triple (namespace, p, s);
CREATE INDEX IF NOT EXISTS coa_triple_label_trgm
    ON coa_triple USING GIN (lower(o) gin_trgm_ops) WHERE o_kind = 'literal';
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
-- Added by the Aurora search layer (coa_common.aurora_search). 'namespace' holds the
-- AOSS-equivalent index name (the search scope, e.g. coa_common.constants
-- ontology_vector_index_name / graphrag_index_names). The HNSW index on
-- embedding is NOT created here: the vector dimension is runtime-configured,
-- so AuroraSearchIndex.ensure_vector_index() creates
-- search_doc_embedding_hnsw_<dim> on the write path.
ALTER TABLE search_doc ADD COLUMN IF NOT EXISTS doc_kind TEXT NOT NULL DEFAULT 'doc';
ALTER TABLE search_doc ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS search_doc_metadata_idx ON search_doc USING GIN (metadata jsonb_path_ops);

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

-- ── Fuzzy and prefix matching (counterparty / entity names) ────────────────
-- Needed because tsvector alone cannot match "Glencor" to "Glencore" or
-- "Harbour" to "Harbor". Labels and aliases get a trigram index; the trigram
-- similarity score is a third input to the reciprocal rank fusion.
CREATE TABLE IF NOT EXISTS entity_label (
    entity_id  TEXT NOT NULL,
    namespace  TEXT NOT NULL,
    label      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'label',          -- 'label' | 'alias'
    PRIMARY KEY (namespace, entity_id, label)
);
-- Existing tables were created with PRIMARY KEY (entity_id, label), which let one
-- namespace overwrite another's labels. Re-key idempotently.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.conrelid = 'entity_label'::regclass
          AND c.contype = 'p'
          AND cardinality(c.conkey) = 2
    ) THEN
        ALTER TABLE entity_label DROP CONSTRAINT entity_label_pkey;
        ALTER TABLE entity_label ADD PRIMARY KEY (namespace, entity_id, label);
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS entity_label_trgm_idx
    ON entity_label USING GIN (label gin_trgm_ops);
--   fuzzy:  SELECT entity_id, similarity(label, $1) AS s FROM entity_label
--           WHERE label % $1 AND namespace = $2 ORDER BY s DESC LIMIT 50;
--   prefix: to_tsquery('english', 'harb:*') matches partial words in search_doc.tsv.

-- ── Search roles (least privilege) ─────────────────────────────────────────
-- coa_search_rw: ingest, metric API, import worker, ontology, namespace deletion.
-- coa_search_ro: serve (read-only search).
-- coa_engine_rw: ontology engine (triples, metadata mirror) and namespace deletion. Member of coa_search_rw,
-- so it also reads and writes the search tables.
-- Passwords are set by the migrator custom resource from each role's Secrets
-- Manager secret, never from this file.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coa_search_rw') THEN
        CREATE ROLE coa_search_rw LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coa_search_ro') THEN
        CREATE ROLE coa_search_ro LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coa_engine_rw') THEN
        CREATE ROLE coa_engine_rw LOGIN;
    END IF;
END
$$;
GRANT USAGE ON SCHEMA public TO coa_search_rw, coa_search_ro, coa_engine_rw;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON search_doc, search_corpus_stats, search_term_stats, entity_label TO coa_search_rw;
GRANT SELECT
    ON search_doc, search_corpus_stats, search_term_stats, entity_label TO coa_search_ro;
GRANT coa_search_rw TO coa_engine_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON coa_triple, ontology_engine TO coa_engine_rw;
-- bigserial columns need USAGE on the sequence for INSERT. Default privileges cover tables created later.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO coa_search_rw, coa_search_ro, coa_engine_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO coa_search_rw, coa_search_ro, coa_engine_rw;
