# Aurora search path

An alternative to OpenSearch Serverless (AOSS) for vector, keyword and fuzzy
search, backed by the Aurora PostgreSQL cluster (pgvector, tsvector, pg_trgm).
It is opt-in. With default settings nothing changes and AOSS is used.

Code: `libs/common/src/coa_common/aurora_search/`. Schema:
`infra/lib/stacks/foundation/migrations/aurora-init.sql`.

## Backend selection

| Setting | Effect |
|---|---|
| (nothing set) | AOSS. No SSM or Aurora call is made. |
| `HYBRID_SEARCH_BACKEND=pgvector+tsvector` (or `pgvector`) | Aurora search. Wins over `WORKBENCH_BACKEND`. |
| `WORKBENCH_BACKEND=aurora_pgvector` | Aurora search, and the ontology-engine store factory returns `AuroraVectorStore`. |

Any other value of `HYBRID_SEARCH_BACKEND`, including empty, means AOSS.

CDK: the search wiring is emitted only when all three flags are set:
`graph_backend=aurora`, `vector_backend=pgvector` and `metadata_backend=aurora`
(`isAuroraSearchEnabled` in `infra/lib/utils/aurora-search.ts`). That is the
condition under which the cluster exists. The searching services then get
`HYBRID_SEARCH_BACKEND=pgvector+tsvector`, `AURORA_SSM_PREFIX`, the Data API
permissions and one role secret (see below). With `vector_backend=pgvector`
alone nothing is emitted and the stack adds a CDK warning that the search layer
needs all three flags. With the defaults (`vector_backend=aoss`) the synthesized
templates contain none of it. The ontology ECS task also gets
`WORKBENCH_BACKEND=aurora_pgvector` on the full-flag path.

### OpenSearch Serverless for GraphRAG on the full-flag path

The GraphRAG toolkit still writes its `chunk_*`, `topic_*` and `statement_*`
indexes to AOSS. The full-flag path therefore keeps a small AOSS collection
(the same collection group and collection as the default path, min OCU 0 and
max OCU 4 unless `aoss_min_ocu` / `aoss_max_ocu` are set) and the `aoss-data`
VPC endpoint. `{ssmPrefix}/opensearch/endpoint`, `/collection-name` and
`/collection-arn` carry the real AOSS values, never the Aurora address, so
namespace deletion drops the GraphRAG indexes as before. In serve,
`build_vector_client` returns a `RoutingVectorClient`
(`coa_serve/clients/routing.py`) when an OpenSearch endpoint is configured:
index names starting with `chunk_`, `topic_` or `statement_` go to AOSS and
every other scope goes to Aurora. With min OCU 0 the collection can scale to
zero, so the first GraphRAG query after an idle period is slower.

Later phase: GraphRAG chunk ingestion to Aurora. Once the toolkit writes chunks
to Aurora, the AOSS collection and endpoint on this path can be removed.

Services wired: ontology (ECS task, engine Lambda, task role), serve
(AgentCore runtime and its role), metric-service (API and import-worker
Lambdas) and the namespace-deletion pipeline (`DeleteSources` Lambda, which runs
the Aurora scope cleanup).

Connection settings: `AURORA_CLUSTER_ARN` if set, else SSM
`{AURORA_SSM_PREFIX}/aurora/cluster-arn`. `AURORA_SECRET_ARN` is required; there
is no fallback to the admin secret (`/aurora/secret-arn`), and a missing value
raises `RuntimeError`.
`AURORA_DATABASE` defaults to `postgres`. An `AURORA_SECRET_ARN` value starting
with `/` is treated as an SSM parameter name; the CDK sets it to
`{ssmPrefix}/aurora/search-rw-secret-arn` or `/aurora/search-ro-secret-arn`.

Vector dimensions come from `AuroraConfig.dimensions`: `AURORA_VECTOR_DIMENSIONS`,
then `OSS_DIMENSIONS`, then 1024. Every writer and reader takes the dimension
from there. Values outside 1..2000 are rejected (pgvector HNSW on `vector`
supports at most 2000). In `libs/common` a vector of the wrong length raises
`ValueError`. The ontology `AuroraVectorStore` follows the `VectorStore`
contract (`stores/base.py`), as the OpenSearch store does: it zero-pads a vector
shorter than the dimension and logs one warning per process when it does. A
longer vector raises `ValueError`.

On the full-flag path the CDK uses one value, `bedrockEmbedDimensions` from the
deploy config, for the migrator (`VectorDimensions`, which names and builds the
HNSW index) and sets `AURORA_VECTOR_DIMENSIONS` and `OSS_DIMENSIONS` to it on
the ontology task and engine Lambda, the serve runtime, and the metric API and
import-worker Lambdas. A deployment that sets `AURORA_VECTOR_DIMENSIONS` by hand
must set `OSS_DIMENSIONS` to the same value for the embedders.

### Per-role credentials

Search workloads do not use the cluster admin secret. `aurora-init.sql` creates
two Postgres roles and `AuroraPg` generates a Secrets Manager secret for each:

| Role | Rights on the four search tables | Used by |
|---|---|---|
| `coa_search_rw` | SELECT, INSERT, UPDATE, DELETE | ontology, metric API, import worker, DeleteSources |
| `coa_search_ro` | SELECT | serve |

The four tables are `search_doc`, `search_corpus_stats`, `search_term_stats`
and `entity_label`. IAM per workload (`grantAuroraDataApi(..., role)`): the
Data API actions on the cluster ARN, `secretsmanager:GetSecretValue` on that
role's secret only, and `ssm:GetParameter` on the two exact parameters it reads
(`cluster-arn` and `search-<role>-secret-arn`). No wildcards. The admin secret
stays with the migrator; no search workload, the ontology task included, is
granted it.

### Schema migrator

A Lambda-backed custom resource (`infra/lib/lambdas/aurora-migrator`, outside a
VPC, Data API only) applies `aurora-init.sql` when the cluster is created and
whenever the SHA-256 of the file changes. It connects with the admin secret,
runs the idempotent DDL (extensions, tables, the `entity_label` re-key, roles
and grants) and owns the HNSW index. The Lambda timeout is 15 minutes.

- DDL runs with `continueAfterTimeout=True`. After each statement the migrator
  polls `pg_stat_activity` until no other DDL of the same user is still active,
  bounded by the Lambda's remaining time minus 30 s.
- The index `search_doc_embedding_hnsw_<dim>` is built with `CREATE INDEX
  CONCURRENTLY IF NOT EXISTS`. An invalid leftover of the same name is dropped
  first, and the migrator then polls `pg_index.indisvalid` until the index is
  valid (same time bound). After that it drops every other
  `search_doc_embedding_hnsw_<n>` index, so a dimension change leaves no stale
  index behind.
- Role passwords are set through `coa_set_role_password(name, pw)`, a
  `SECURITY DEFINER` function in `aurora-init.sql` that accepts only
  `coa_search_rw` and `coa_search_ro` and is revoked from `PUBLIC`. The migrator
  calls `SELECT coa_set_role_password(:name, :pw)` with bound parameters, so no
  SQL text it sends carries a password. Passwords never appear in the template.
- `aurora-init.sql` must not contain bind parameters (`:name`), tagged dollar
  quotes (`$tag$`) or `E'...'` strings. `split_statements` raises `ValueError`
  on the last two.

The alternative is to run the SQL once by hand through the Data API. The custom
resource costs an extra Lambda and gives it admin-secret access.

## Data model

Documents live in `search_doc`. The `namespace` column holds the scope, which is
the index name AOSS would have used. `doc_id` is a hash of scope, kind, entity
URI, embedding type, model and ontology, so re-ingesting a document overwrites
it (AOSS appends). `metadata` (JSONB) carries filterable fields. `search_term_stats`
and `search_corpus_stats` hold per-scope term document frequencies and corpus
size. `entity_label` holds entity names and aliases for fuzzy matching, keyed by
`(namespace, entity_id, label)`. `SearchDoc.label` and `aliases` default to
`None`, meaning no label information: the entity's labels are left alone. A
document that sets either one is authoritative: the entity's labels in the
scope become the union over its authoritative documents, and an empty union
deletes them.

Re-index requirement: `entity_label` used to be keyed by `(entity_id, label)`,
so two namespaces that shared an entity and label kept only one row. The re-key
in `aurora-init.sql` keeps the existing rows but cannot restore the lost ones.
Namespaces ingested under the old key must be re-indexed to get their labels
back.

The HNSW index (`search_doc_embedding_hnsw_<dim>`, cosine) is owned by the
migrator. The write path checks `pg_indexes`. If the index is missing it tries
to create it; when that fails with `must be owner` (`coa_search_rw` does not own
the table) it logs `aurora_hnsw_index_missing` at ERROR and checks again on the
next write. Reads never create indexes.

## Search

1. **Recall.** `to_tsquery('english', ...)` over `search_doc.tsv`. Query tokens
   are lowercased and capped at 32. Word characters are kept in any script, and
   each lexeme is quoted inside `to_tsquery` (quote and backslash escaped),
   joined with `|`, each with a `:*` prefix marker. Candidates are ordered by
   `ts_rank`, 200 by default. When the ordinary tsquery is empty (phrase-only
   search) the rank comes from the phrase tsqueries, so the recall cap keeps the
   best-ranked matches.
2. **BM25 re-score** in Python on the recalled candidates. `idf = ln(1 + (N - df
   + 0.5) / (df + 0.5))`, `k1 = 1.2`, `b = 0.75`. N, df and average length come
   from the stats tables. A prefix query term matches every candidate lexeme that
   starts with it: tf is summed per candidate, and df is the maximum over the
   expansions, computed once per query term. A term with no row in
   `search_term_stats` gets zero weight. Query lexemes come from
   Postgres (`to_tsvector('english', q)`) so stemming matches the index.
   BM25 is computed over the recall set, so it is a re-ranker, not full-corpus
   BM25 as in a search engine.
3. **Phrases** use `phraseto_tsquery` per phrase and report which matched.
4. **Fuzzy names** use `pg_trgm` `similarity()` on `entity_label` (threshold 0.3),
   with an optional `levenshtein` re-check. The caller's filters apply to this
   leg too.
5. **Fusion.** Reciprocal rank fusion, `score = sum 1/(60 + rank)`, over the
   vector, BM25 and trigram rankings. Ties break by key. Modes: `hybrid` (all
   three), `semantic` (vector only), `lexical` (BM25 only). Hybrid without a
   query vector degrades to lexical.

Scores: the ontology store returns raw cosine in [-1, 1]. The serve client and
metric client return `(1 + cosine) / 2` so thresholds written for AOSS apply.

## Statistics upkeep

Every upsert and delete runs in a transaction and applies net term and corpus
deltas (document counts, lengths, term frequencies) computed from the stored
`tsvector`. Each writer first takes
`pg_advisory_xact_lock(hashtextextended(scope, 0))`, so writers of one scope are
serialised and the statistics stay equal to a recount. `delete_docs` selects a
batch `FOR UPDATE` (halving the batch on the 1 MiB response limit), applies the
deltas for those rows and deletes them by id, with no `RETURNING`. Rows with
`doc_freq <= 0` are removed, and corpus counts are clamped at 0. `delete_scope`
runs one transaction per 5000-row batch (Data API calls time out near 45 s), so
progress survives a timeout. A last transaction checks that the scope has no
documents left, then clears its stats and label rows; if documents reappeared,
it loops again. Searches take no lock.

Keyword recall projects only the ranking columns and halves the candidate count
(floor 10) on the 1 MiB response limit. `body` and `metadata` are fetched
afterwards for the top results only, in batches that halve on the same error. Namespace
deletion calls `delete_scope` for each GraphRAG index scope.

The Data API client keeps the transaction id per thread, creates the boto client
under a lock, batches at 500 parameter sets or 2 MiB (one parameter set over the
limit is rejected locally), and retries up to about 60 s while a paused cluster
resumes. `iter_pages(sql, params, page_size, key_columns=("doc_id",))` uses
keyset paging: it wraps the statement as `SELECT * FROM (<sql>) q WHERE (keys) >
(last keys) ORDER BY keys LIMIT n`, with no `OFFSET`, and halves the page size on
the 1 MiB response limit. Key columns must be non-null output columns of the
statement. The ontology store lists use it.

## Running the Postgres integration test

Opt-in. It needs a local Postgres with `vector` and `pg_trgm`, and the driver
(`pip install -e "libs/common[integ]"`). Run a container yourself, for example:

```
docker run -d --name coa-pg -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg16
```

then set `COA_PG_TEST_DSN` and run
`pytest -m integ libs/common/tests/integ`. Without the variable the test is
skipped. The fixture applies `aurora-init.sql` itself and drops only the
`CREATE EXTENSION ... pgrouting` line, because the image has no pgrouting. The
tests cover concurrent writers, statistics against a recount, keyset paging,
phrase recall over the cap, namespace-keyed and authoritative labels, scope
deletion, the HNSW index probe, the per-role grants, `coa_set_role_password`
and the `entity_label` re-key.

## Not covered or not verified yet

Verified on a real PostgreSQL 16 with pgvector (the integration tests, 29 cases)
and, for everything else, with mocked Data API clients, Python unit tests and CDK
Jest synthesis tests:

- Nothing ran on Aurora or through the RDS Data API. Data API JSON typing, the
  1 MiB and 45 s limits, IAM, the custom resource and the Aurora PostgreSQL
  version are unverified. The integration test uses psycopg, not the Data API.
- The migrator's `continueAfterTimeout`, `pg_stat_activity` and `indisvalid`
  waits, and the AOSS collection, endpoint and routing on the full-flag path,
  are verified only with mocks and CDK synthesis.
- No deployment or end-to-end search was done. Relevance versus AOSS is unmeasured.

Not wired:

- GraphRAG toolkit chunk ingestion talks to AOSS directly on both paths (see
  above). The Aurora tables and client can hold chunk rows, but nothing writes
  them. Namespace deletion deletes the AOSS GraphRAG indexes and clears the
  Aurora scopes.
- Sources stack `graph_build` and `graph_cleanup`, which use the toolkit. The
  sources stack is unchanged for that reason.
- `aoss-search-proxy` Lambda (AOSS only).
- Graph traversal, the DynamoDB metadata swap, VKG, and skill embeddings (no
  skill index exists).
- Fuzzy matching only applies where entity labels are supplied.
