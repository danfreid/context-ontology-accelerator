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

CDK: `-c vector_backend=pgvector` sets `HYBRID_SEARCH_BACKEND=pgvector+tsvector`
and `AURORA_SSM_PREFIX` on the services that search, and grants them the Data
API and secret permissions (`infra/lib/utils/aurora-search.ts`). With
`vector_backend=aoss` (default) the synthesized templates contain neither.
Services wired: ontology (ECS task, engine Lambda, task role), serve
(AgentCore runtime and its role), metric-service (API and import-worker
Lambdas) and the namespace-deletion pipeline (`DeleteSources` Lambda, which runs
the Aurora scope cleanup).

Connection settings: `AURORA_CLUSTER_ARN` and `AURORA_SECRET_ARN` if set, else
SSM `{AURORA_SSM_PREFIX}/aurora/cluster-arn` and `/aurora/secret-arn`.
`AURORA_DATABASE` defaults to `postgres`. Vector dimensions come from
`AURORA_VECTOR_DIMENSIONS`, then `OSS_DIMENSIONS`, then 1024. An
`AURORA_SECRET_ARN` value starting with `/` is treated as an SSM parameter name.

IAM granted on the pgvector path: `rds-data:ExecuteStatement`,
`BatchExecuteStatement`, `BeginTransaction`, `CommitTransaction`,
`RollbackTransaction` on the cluster ARN; `secretsmanager:GetSecretValue` on the
secret ARN; `ssm:GetParameter(s)` on `{ssmPrefix}/aurora/*`.

## Data model

Documents live in `search_doc`. The `namespace` column holds the scope, which is
the index name AOSS would have used. `doc_id` is a hash of scope, kind, entity
URI, embedding type, model and ontology, so re-ingesting a document overwrites
it (AOSS appends). `metadata` (JSONB) carries filterable fields. `search_term_stats`
and `search_corpus_stats` hold per-scope term document frequencies and corpus
size. `entity_label` holds entity names and aliases for fuzzy matching.

The HNSW index (`search_doc_embedding_hnsw_<dim>`, cosine) is created by the
write path because the dimension is configured at runtime. Reads never create
indexes.

## Search

1. **Recall.** `to_tsquery('english', ...)` over `search_doc.tsv`. Query tokens
   are lowercased, capped at 32, and sanitized to `[a-z0-9_]`, joined with `|`,
   each with a `:*` prefix marker. Candidates are ordered by `ts_rank`, 200 by
   default.
2. **BM25 re-score** in Python on the recalled candidates. `idf = ln(1 + (N - df
   + 0.5) / (df + 0.5))`, `k1 = 1.2`, `b = 0.75`. N, df and average length come
   from the stats tables. A prefix query term matches every candidate lexeme that
   starts with it: tf is summed, df is the maximum. Query lexemes come from
   Postgres (`to_tsvector('english', q)`) so stemming matches the index.
   BM25 is computed over the recall set, so it is a re-ranker, not full-corpus
   BM25 as in a search engine.
3. **Phrases** use `phraseto_tsquery` per phrase and report which matched.
4. **Fuzzy names** use `pg_trgm` `similarity()` on `entity_label` (threshold 0.3),
   with an optional `levenshtein` re-check.
5. **Fusion.** Reciprocal rank fusion, `score = sum 1/(60 + rank)`, over the
   vector, BM25 and trigram rankings. Ties break by key. Modes: `hybrid` (all
   three), `semantic` (vector only), `lexical` (BM25 only). Hybrid without a
   query vector degrades to lexical.

Scores: the ontology store returns raw cosine in [-1, 1]. The serve client and
metric client return `(1 + cosine) / 2` so thresholds written for AOSS apply.

## Statistics upkeep

Every upsert and delete runs in a transaction and applies net term and corpus
deltas (document counts, lengths, term frequencies) computed from the stored
`tsvector`. Rows with `doc_freq <= 0` are removed. `delete_scope` deletes in
5000-row batches (Data API calls time out near 45 s), then clears the scope's
stats and label rows. Namespace deletion calls it for each GraphRAG index scope.

The Data API client batches at 500 parameter sets or 2 MiB, pages large reads,
halves the page size on the 1 MiB response limit, and retries up to about 60 s
while a paused cluster resumes.

## Running the Postgres integration test

Opt-in. It needs a local Postgres with `vector` and `pg_trgm`. Run a container
yourself, for example:

```
docker run -d --name coa-pg -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg16
```

then create the extensions, set `COA_PG_TEST_DSN`, and run
`pytest -m integ libs/common/tests/integ`. Without the variable the test is skipped.

## Not covered or not verified yet

Verified only with mocked Data API clients, Python unit tests and CDK Jest
synthesis tests:

- No SQL has been run against a real Postgres or Aurora cluster. The integration
  test exists but was never executed. `aurora-init.sql` changes were not applied.
- No deployment or end-to-end search was done. Relevance versus AOSS is unmeasured.
- HNSW on `vector(<dim>)` supports at most 2000 dimensions in pgvector.

Not wired:

- GraphRAG toolkit chunk ingestion and retrieval talk to AOSS directly. The
  Aurora tables and client can hold chunk rows, but nothing writes them.
- Sources stack `graph_build` and `graph_cleanup`, which use the toolkit. The
  sources stack is unchanged for that reason.
- `aoss-search-proxy` Lambda (AOSS only).
- Graph traversal, the DynamoDB metadata swap, VKG, and skill embeddings (no
  skill index exists).
- Fuzzy matching only applies where entity labels are supplied.
- `entity_label` is keyed by `(entity_id, label)` without scope, so the same
  entity ID in two scopes shares rows.
- The ontology ECS task role gets the Data API grants again when
  `vector_backend=pgvector` and the Aurora graph and metadata flags are also set
  (duplicate statements, same resources).
