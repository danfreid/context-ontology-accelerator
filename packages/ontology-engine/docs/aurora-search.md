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

Services wired: ontology (ECS task, engine Lambda, task role), serve
(AgentCore runtime and its role), metric-service (API and import-worker
Lambdas) and the namespace-deletion pipeline (`DeleteSources` Lambda, which runs
the Aurora scope cleanup).

Connection settings: `AURORA_CLUSTER_ARN` and `AURORA_SECRET_ARN` if set, else
SSM `{AURORA_SSM_PREFIX}/aurora/cluster-arn` and the secret parameter.
`AURORA_DATABASE` defaults to `postgres`. An `AURORA_SECRET_ARN` value starting
with `/` is treated as an SSM parameter name; the CDK sets it to
`{ssmPrefix}/aurora/search-rw-secret-arn` or `/aurora/search-ro-secret-arn`.

Vector dimensions come from `AuroraConfig.dimensions`: `AURORA_VECTOR_DIMENSIONS`,
then `OSS_DIMENSIONS`, then 1024. Every writer and reader takes the dimension
from there. Values outside 1..2000 are rejected (pgvector HNSW on `vector`
supports at most 2000), and a vector of the wrong length raises `ValueError`
instead of being truncated or padded. A deployment that sets
`AURORA_VECTOR_DIMENSIONS` must set `OSS_DIMENSIONS` to the same value for the
embedders.

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
stays with the graph and metadata path and the migrator.

### Schema migrator

A Lambda-backed custom resource (`infra/lib/lambdas/aurora-migrator`, outside a
VPC, Data API only) applies `aurora-init.sql` when the cluster is created and
whenever the SHA-256 of the file changes. It connects with the admin secret,
runs the idempotent DDL (extensions, tables, the `entity_label` re-key, roles
and grants) and sets each role password from its generated secret with a bound
parameter and server-side `format('%I', '%L')`. Passwords never appear in the
template. `aurora-init.sql` must not contain bind parameters (`:name`).

The alternative is to run the SQL once by hand through the Data API. The custom
resource costs an extra Lambda and gives it admin-secret access.

## Data model

Documents live in `search_doc`. The `namespace` column holds the scope, which is
the index name AOSS would have used. `doc_id` is a hash of scope, kind, entity
URI, embedding type, model and ontology, so re-ingesting a document overwrites
it (AOSS appends). `metadata` (JSONB) carries filterable fields. `search_term_stats`
and `search_corpus_stats` hold per-scope term document frequencies and corpus
size. `entity_label` holds entity names and aliases for fuzzy matching, keyed by
`(namespace, entity_id, label)`. An upsert deletes the entity's labels that are
no longer in its label and alias set.

The HNSW index (`search_doc_embedding_hnsw_<dim>`, cosine) is created by the
write path because the dimension is configured at runtime. Reads never create
indexes.

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
serialised and the statistics stay equal to a recount. `delete_docs` applies
deltas only for the rows its `DELETE ... RETURNING` removed. Rows with
`doc_freq <= 0` are removed. `delete_scope` runs in one transaction under the
lock, deletes documents in 5000-row batches (Data API calls time out near 45 s),
then clears the scope's stats and label rows. Searches take no lock. Namespace
deletion calls `delete_scope` for each GraphRAG index scope.

The Data API client keeps the transaction id per thread, creates the boto client
under a lock, batches at 500 parameter sets or 2 MiB (one parameter set over the
limit is rejected locally), and retries up to about 60 s while a paused cluster
resumes. `iter_pages` pages large reads with a caller-supplied total `ORDER BY`
and halves the page size on the 1 MiB response limit. The ontology store lists
use it.

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
test covers concurrent writers, statistics against a recount, paging, phrase
recall over the cap and namespace-keyed labels.

## Not covered or not verified yet

Verified on a real PostgreSQL 16 with pgvector (the integration test, 12 cases)
and, for everything else, with mocked Data API clients, Python unit tests and CDK
Jest synthesis tests:

- Nothing ran on Aurora or through the RDS Data API. Data API JSON typing, the
  1 MiB and 45 s limits, IAM, the custom resource and the Aurora PostgreSQL
  version are unverified. The integration test uses psycopg, not the Data API.
- `ensure_vector_index` runs `CREATE INDEX` from the write path. On a real
  server the `coa_search_rw` role is rejected with `must be owner of table
  search_doc`, because it does not own the table. The first write with an
  embedding fails under that role until an owner creates the index.
- No deployment or end-to-end search was done. Relevance versus AOSS is unmeasured.

Not wired:

- GraphRAG toolkit chunk ingestion and retrieval talk to AOSS directly. The
  Aurora tables and client can hold chunk rows, but nothing writes them.
  Namespace deletion therefore still deletes the AOSS GraphRAG indexes as well as
  clearing the Aurora scopes.
- Sources stack `graph_build` and `graph_cleanup`, which use the toolkit. The
  sources stack is unchanged for that reason.
- `aoss-search-proxy` Lambda (AOSS only).
- Graph traversal, the DynamoDB metadata swap, VKG, and skill embeddings (no
  skill index exists).
- Fuzzy matching only applies where entity labels are supplied.
- The ontology ECS task role keeps its admin-secret grant for the graph and
  metadata path. Search itself uses the rw role.
