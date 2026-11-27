# Aurora PostgreSQL backend and search path

How the ontology engine keeps its RDF graph, its embeddings, its own metadata
and the hybrid search index in Aurora PostgreSQL through the RDS Data API, how
that maps onto the Neptune, OpenSearch Serverless (AOSS) and DynamoDB backends,
and what is still open.

Audience: a developer who knows SQL and AWS and has read the backend section of
the package README. Terms such as named graph and T-Box are explained in
[`ontology-data-model.md`](ontology-data-model.md).

It is opt-in. With default settings nothing changes: Neptune, AOSS and DynamoDB
are used.

Primary sources:

- `libs/common/src/coa_common/aurora_data_api/`: the one shared Data API client (`AuroraDataApiClient`, `AuroraConfig`, typed errors)
- `libs/common/src/coa_common/aurora_search/`: search index (vector, BM25, trigram, fusion)
- `src/coa_ontology/stores/aurora/graph.py`: `AuroraGraphStore`
- `src/coa_ontology/stores/aurora/schema.py`: read-only schema check, dev-only DDL
- `src/coa_ontology/stores/aurora_vector.py`: `AuroraVectorStore` on `search_doc`
- `src/coa_ontology/aurora_metadata.py`: DynamoDB shim for the engine table
- `src/coa_ontology/stores/factory.py`: backend selection
- `infra/lib/stacks/foundation/migrations/aurora-init.sql`: every table, role and grant

## 1. Architecture

One Aurora cluster holds four things: the RDF graph (`coa_triple`), the
embeddings and search documents (`search_doc` and its statistics tables), the
engine's DynamoDB single-table metadata (`ontology_engine`: registry, jobs,
locks, proposals) and fuzzy entity labels (`entity_label`).

Every service connects through the RDS Data API (`boto3` `rds-data`) with one
client class, `coa_common.aurora_data_api.AuroraDataApiClient`. There is no
Postgres driver and no connection pool, and the engine side of the connection
needs no VPC, endpoint or NAT gateway. The client:

- maps service errors to `AuroraQueryError`, `AuroraAccessError`,
  `AuroraUnavailableError` (`ResumeTimeoutError` is one), `ResponseTooLargeError`
  and `StatementTimeoutError`, keeping the botocore `ClientError` as `__cause__`;
- retries a resuming cluster (min ACU 0) with backoff and jitter within
  `AURORA_RESUME_TIMEOUT_SECONDS`;
- uses an explicit transaction handle (`with client.transaction() as tx: tx.execute(...)`);
- pages with keyset `iter_pages(build, page_size)` / `query_paged`, halving the
  page on the 1 MiB response limit;
- sends lists and dicts as JSON strings with `typeHint: JSON`.

`WORKBENCH_BACKEND=aurora_pgvector` makes `build_stores()` return
`(AuroraGraphStore, AuroraVectorStore)`. The same value turns on Aurora search
(`resolve_search_backend`) and, unless `METADATA_BACKEND` says otherwise, the
Aurora metadata shim.

## 2. Backend selection

| Setting | Effect |
|---|---|
| (nothing set) | Neptune DB graph, AOSS vectors and search, DynamoDB metadata. No SSM or Aurora call is made. |
| `WORKBENCH_BACKEND=aurora_pgvector` | `build_stores()` returns `(AuroraGraphStore, AuroraVectorStore)`; Aurora search; metadata in Aurora unless `METADATA_BACKEND=dynamodb`. |
| `HYBRID_SEARCH_BACKEND=pgvector+tsvector` (or `pgvector`) | Aurora search. Wins over `WORKBENCH_BACKEND`. Any other value, including empty, means AOSS. |
| `METADATA_BACKEND=aurora` or `dynamodb` | Where `dynamo_store` keeps its items. When unset it is `aurora` only if `WORKBENCH_BACKEND=aurora_pgvector`. Any other value raises `ValueError`. |

An unknown `WORKBENCH_BACKEND` raises `ValueError` listing `opensearch_neptune`,
`na_only` and `aurora_pgvector`. "Neptune graph plus Aurora vectors" is not
offered under `aurora_pgvector`; it would need its own value.

CDK: the Aurora wiring is emitted only when all three flags are set:
`graph_backend=aurora`, `vector_backend=pgvector` and `metadata_backend=aurora`
(`isAuroraSearchEnabled` in `infra/lib/utils/aurora-search.ts`). That is the
condition under which the cluster exists. With `vector_backend=pgvector` alone
nothing is emitted and the stack adds a CDK warning. With the defaults the
synthesized templates contain none of it.

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

Services wired: ontology (ECS task, engine Lambda, task role; role
`engine-rw`, plus `WORKBENCH_BACKEND=aurora_pgvector` and
`METADATA_BACKEND=aurora`), serve (AgentCore runtime; role `ro`),
metric-service (API and import-worker Lambdas; role `rw`) and the
namespace-deletion pipeline (`DeleteSources` Lambda; role `engine-rw`, because
it also deletes graph and metadata rows).

## 3. Environment variables

| Variable | Meaning |
|---|---|
| `AURORA_SSM_PREFIX` | Fallback for the cluster ARN only: `{prefix}/aurora/cluster-arn`. The CDK sets it. There is no fallback for the secret. |
| `AURORA_CLUSTER_ARN` | Cluster ARN (preferred). `AURORA_RESOURCE_ARN` is accepted as an alias. A value starting with `/` is read as an SSM name. |
| `AURORA_CLUSTER_ENDPOINT` | Used only when no ARN is set and the value starts with `/`: the sibling SSM parameter `<same prefix>/cluster-arn` supplies the ARN. |
| `AURORA_SECRET_ARN` | Required. A missing value raises `AuroraConfigError`; there is no fallback to the admin secret (`/aurora/secret-arn`), so a workload cannot silently pick it up. Secrets Manager ARN used as is when it starts with `arn:`; otherwise an SSM parameter name. The CDK sets `{ssmPrefix}/aurora/search-rw-secret-arn`, `/aurora/search-ro-secret-arn` or `/aurora/engine-rw-secret-arn`. The Data API reads the secret itself; services never see credentials. |
| `AURORA_DATABASE` | Database name. Default `postgres`. |
| `AURORA_VECTOR_DIMENSIONS` | Embedding width. Falls back to `OSS_DIMENSIONS`, then 1024. Range 1 to 2000 (HNSW limit), else `AuroraConfigError`. A deployment that sets it must set `OSS_DIMENSIONS` to the same value for the embedders. |
| `AURORA_RESUME_TIMEOUT_SECONDS` | Retry budget for a paused cluster. Default 45. |
| `WORKBENCH_BACKEND`, `HYBRID_SEARCH_BACKEND`, `METADATA_BACKEND` | See section 2. |
| `NDB_GRAPH_URI_BASE` | Reused from the Neptune store: the named-graph IRI base. |

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

SSM reads are lazy, cached per process, and need `ssm:GetParameter` on the
exact parameter. When the grant is missing the error names it.

## 4. Schema

`infra/lib/stacks/foundation/migrations/aurora-init.sql` owns every table,
index, role and grant. The aurora-migrator custom resource applies it (see
section 5). At runtime nothing creates tables:
`stores/aurora/schema.py:check_schema` runs once per client and only reads. It
confirms that `coa_triple`, `ontology_engine` and `search_doc` exist and that
vectors already stored in `search_doc` have the configured width, and otherwise
raises `AuroraSchemaError` naming `aurora-init.sql`.
`apply_schema_for_dev()` creates the two engine tables on a bare database for
local development; neither store calls it.

| Table | Content | Indexes |
|---|---|---|
| `coa_triple` | One row per quad: `id bigserial, namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype`. | Unique `(graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype)` gives RDF set semantics. Covering `(namespace, s, p) INCLUDE (o, graph_uri)` and `(namespace, o, p) INCLUDE (s, graph_uri)`, both partial on `o_kind='iri'`. `(namespace, p, s)`. Trigram GIN on `lower(o)` for literals. |
| `ontology_engine` | `(pk, sk, attributes jsonb)`, the DynamoDB mirror. | Primary key; `pk text_pattern_ops` for `begins_with`. |
| `search_doc` | `doc_id, namespace` (the scope), `body, doc_len, tsv, embedding vector, doc_kind, metadata jsonb`. Ontology embeddings have `doc_kind='ontology'`. | GIN on `tsv`, `namespace`, GIN `jsonb_path_ops` on `metadata`; HNSW `search_doc_embedding_hnsw_<dim>` (cosine, partial expression index) created by the migrator. |
| `search_corpus_stats`, `search_term_stats` | Per-scope corpus size and term document frequencies for BM25. | Primary keys. |
| `entity_label` | Entity names and aliases for fuzzy matching, keyed by `(namespace, entity_id, label)`. | Trigram GIN on `label`. |

`md5(o)` in the unique key keeps long literals out of the btree (entries over
about 2.7 KB fail). The vector column has no fixed width; a width change needs a
re-embed into a new index.

## 5. Roles and migrator

Services do not use the cluster admin secret. `aurora-init.sql` creates three
login roles and `AuroraPg` generates a Secrets Manager secret for each:

| Role | Rights | Used by |
|---|---|---|
| `coa_search_ro` | SELECT on the four search tables | serve |
| `coa_search_rw` | SELECT, INSERT, UPDATE, DELETE on the four search tables | metric API, import worker |
| `coa_engine_rw` | Member of `coa_search_rw`, plus SELECT, INSERT, UPDATE, DELETE on `coa_triple` and `ontology_engine` | ontology engine (ECS task and Lambda), namespace deletion |

All three get `USAGE, SELECT` on the sequences in `public`, including future
ones through `ALTER DEFAULT PRIVILEGES`. IAM per workload
(`grantAuroraDataApi(..., role)`): the Data API actions on the cluster ARN,
`secretsmanager:GetSecretValue` on that role's secret only, and
`ssm:GetParameter` on the two exact parameters it reads (`cluster-arn` and the
role's secret parameter). No wildcards. Only the migrator has the admin secret.

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
  `coa_search_rw`, `coa_search_ro` and `coa_engine_rw` and is revoked from `PUBLIC`. The migrator
  calls `SELECT coa_set_role_password(:name, :pw)` with bound parameters, so no
  SQL text it sends carries a password. Passwords never appear in the template.
- `aurora-init.sql` must not contain bind parameters (`:name`), tagged dollar
  quotes (`$tag$`) or `E'...'` strings. `split_statements` raises `ValueError`
  on the last two.

## 6. Graph mapping and traversal

- **Named graphs.** Each ontology is the graph `{base}/{ns}/{quote(ontology_id)}`, built by the Neptune store's `_ontology_graph_uri` helper and stored in `graph_uri`. The `namespace` column is the scope Neptune expresses as the `{base}/{ns}/` prefix. Every read filters on `namespace` first.
- **Turtle in.** `load_turtle` parses with `rdflib` and bulk-inserts rows with `ON CONFLICT DO NOTHING`.
- **Turtle out.** `get_graph_turtle` reads rows paged by `id` and serialises with `rdflib`.
- **Classes and properties.** `store_class`, `store_property` and the batch variants take their triples from the Neptune store's own builders, so both backends store identical triples.
- **Search, overview, schema.** `search_entities`, `count_entities`, `get_vertex`, `get_ontology_overview` and `get_namespace_schema` are SQL statements over `coa_triple`. Label, kind and language choices reuse the Neptune helpers. Search orders with `COLLATE "C"` to match SPARQL codepoint order.
- **Sample overview.** Subtree sizes that rank seed roots come from one recursive `traverse` statement; the 50-descendant breadth-first fill stays the Neptune algorithm in Python.

`AuroraGraphStore.traverse(start_uris, *, predicates, direction, max_depth, namespace, ontology_id, limit, return_paths=False, node_types=None)` is a store-specific method, not part of the `GraphStore` Protocol. It is one `WITH RECURSIVE` statement per call with a mandatory `max_depth`. Plain reachability uses `UNION`, which ends cycles; with `return_paths` it keeps a path array, uses `UNION ALL` and excludes nodes already on the path. pgRouting is not used.

## 7. Vector store and search

`AuroraVectorStore` writes ontology embeddings to `search_doc` through
`AuroraSearchIndex`. The `namespace` column holds the scope, which is the AOSS
index name (`_index_for_namespace`). `doc_id` is a hash of scope, kind, entity
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

Store filters (`entity_type`, `ontology_id`, `embedding_type`, `model_id`)
are `metadata @>` containment. `search_nearest` sets
`hnsw.ef_search = max(40, top_k)` for large `top_k` and
`hnsw.iterative_scan = relaxed_order` for filtered queries. When the top-k
documents exceed 1 MiB it ranks `doc_id, score` first and loads documents 25 at
a time.

Search:

1. **Recall.** `to_tsquery('english', ...)` over `search_doc.tsv`. Query tokens are lowercased and capped at 32, each lexeme quoted and joined with `|` with a `:*` prefix marker. Candidates are ordered by `ts_rank`, 200 by default.
2. **BM25 re-score** in Python on the recalled candidates (`k1 = 1.2`, `b = 0.75`, N, df and average length from the stats tables). It is a re-ranker over the recall set, not full-corpus BM25.
3. **Phrases** use `phraseto_tsquery` per phrase and report which matched.
4. **Fuzzy names** use `pg_trgm` `similarity()` on `entity_label` (threshold 0.3), with an optional `levenshtein` re-check.
5. **Fusion.** Reciprocal rank fusion, `score = sum 1/(60 + rank)`, over the vector, BM25 and trigram rankings. Modes: `hybrid`, `semantic`, `lexical`. Hybrid without a query vector degrades to lexical.

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

The shared Data API client pages with keyset only: `iter_pages(build, page_size)` calls
`build(last_row, limit)` for the SQL and parameters of the next page (`WHERE key > :after ORDER BY
key LIMIT :lim`), with no `OFFSET`, and halves the page on the 1 MiB response limit. Rows inserted or
deleted behind the cursor do not shift later pages. The ontology store lists use it.

## 8. DynamoDB shim

`dynamo_store.py` is written against boto3 `Table` calls with string
expressions. `_get_table()` and `_get_dao()` return `AuroraTable` and
`AuroraDao` when `metadata_backend()` is `aurora`. Items live in
`ontology_engine(pk, sk, attributes)`.

- Unconditional get, put and delete are one statement.
- Conditional put, update and delete run in one transaction: advisory lock on `(pk, sk)`, `SELECT ... FOR UPDATE`, condition check, write. A failed condition raises a botocore `ClientError` with code `ConditionalCheckFailedException`, which `dynamo_store` already catches.
- Query and scan push `begins_with(PK, ...)` and `SK =` to SQL, filter the rest in Python, and honour `Limit`, `ExclusiveStartKey` and `LastEvaluatedKey` by keyset on `(pk, sk)`.
- Conditions: `AND`, `OR`, `NOT`, parentheses, comparisons, `attribute_exists`, `attribute_not_exists`, `begins_with`, `contains`. Updates: `SET` (with `if_not_exists`, `list_append`, arithmetic), `REMOVE`, `ADD`. Anything else raises `NotImplementedError`.
- The API proxy health probe runs `SELECT 1` through the Data API on this path and reports it under the existing `dynamodb` key.

Namespace deletion removes the namespace's `coa_triple` rows and its
`ontology_engine` rows (`pk LIKE '{ns}#%' OR pk = 'NAMESPACE#{ns}'`) in bounded
batches, next to the `search_doc` scope cleanup.

## 9. Data API limits

| Limit | Handling |
|---|---|
| 1 MiB response | `formatRecordsAs=JSON`; `ResponseTooLargeError`. Keyset paging halves the page on that error. Vector search falls back to rank-then-fetch. |
| 4 MiB request | Graph bulk writes send one JSON string parameter per chunk (about 1.5 MiB) and expand it with `jsonb_to_recordset`. `batch_execute` chunks at 500 parameter sets and 2 MiB. |
| 45 s statement | Large deletes run in bounded batches. `StatementTimeoutError` is never retried. |
| No array parameters | Lists go as JSON and expand with `jsonb_array_elements_text`. Vectors go as text and are cast to `vector`. |
| Paused cluster (min ACU 0) | Resume errors retry within `AURORA_RESUME_TIMEOUT_SECONDS`. |

## 10. Decisions

Recorded in `.agents/tasks/aurora-reconcile/report.md` (Q1-Q4):

1. **Q1, client.** One client: A's `DataApiClient` (renamed `AuroraDataApiClient`, alias kept) extended with B's typed errors, `AURORA_SSM_PREFIX`, `dimensions`, JSON parameters and `sync_boto_config`. B's `aurora_search/data_api.py` is gone. Paging is keyset only.
2. **Q2, vector store.** B's `AuroraVectorStore` on `search_doc` (shared with hybrid search and BM25 statistics). A's `coa_embedding` store is dropped; A's `ef_search` widening and oversize fallback moved into `AuroraSearchIndex.vector_search`.
3. **Q3, factory.** `aurora_pgvector` builds `(AuroraGraphStore, AuroraVectorStore)`: A's graph plus B's vector store. One value drives graph, vector, search and metadata.
4. **Q4, schema ownership.** `aurora-init.sql` and the migrator own all DDL. A third role `coa_engine_rw` (member of `coa_search_rw`) serves the engine. Runtime code only checks the schema.

Also: traversal by recursive CTE only; RDF as one quad table with the Neptune
helpers imported rather than copied; the engine's own DynamoDB table moves
behind a shim selected by `METADATA_BACKEND`; other DynamoDB use stays.

Verification status:

The search integration tests ran on a real PostgreSQL 16 with pgvector
and, for everything else, with mocked Data API clients, Python unit tests and CDK
Jest synthesis tests:

- Nothing ran on Aurora or through the RDS Data API. Data API JSON typing, the
  1 MiB and 45 s limits, IAM, the custom resource and the Aurora PostgreSQL
  version are unverified. The integration test uses psycopg, not the Data API.
- The migrator's `continueAfterTimeout`, `pg_stat_activity` and `indisvalid`
  waits, and the AOSS collection, endpoint and routing on the full-flag path,
  are verified only with mocks and CDK synthesis.
- No deployment or end-to-end search was done. Relevance versus AOSS is unmeasured.

## 11. Parity differences

Against Neptune DB, AOSS and DynamoDB (checked by the parity tests):

- **Upsert vs append.** Re-storing an embedding with the same natural key replaces it. AOSS appends duplicates.
- **Cosine score.** `search_nearest` returns raw cosine in [-1, 1], as `base.py` requires. AOSS returns `(1 + cosine) / 2`; the serve and metric clients convert to that so AOSS thresholds still apply.
- **Hits carry no vector**, like the AOSS store. Listings with `limit` omit the vector and keep the metadata keys.
- **Query vector width.** A vector of the wrong length raises `ValueError` instead of being truncated or padded.
- **Search with no match.** Aurora returns an empty page and count 0. The rdflib-backed Neptune reference returns the whole namespace because of an unbound `GROUP BY` row; real Neptune is guarded elsewhere.
- **Several parents.** Neptune's `parentClass` takes an arbitrary parent; Aurora takes the first stored.
- **Grounding.** Aurora folds one row per predicate in priority order (`coa:groundedTo`, `skos:exactMatch`, `skos:closeMatch`), which matches Neptune's preference deterministically.
- **Blank nodes.** Ids are random per parse on every backend; parity masks them.
- **Metadata scan order.** Aurora scans in `(pk, sk)` order under `C` collation; moto's order is undefined, so parity sorts. Moto raises a subclass of `ClientError`; parity compares the error code.

## 12. Tests

From the repo root, with `PYTHONUTF8=1`:

    uv run --frozen pytest packages/ontology-engine -q
    uv run --frozen pytest libs/common -q
    uv run --frozen pytest packages/ontology-engine/tests/integ -rs
    COA_PG_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5432/postgres uv run --frozen --with "psycopg[binary]" pytest libs/common/tests/integ -rs

- **Unit** tests use a scripted `rds-data` client (`tests/unit/aurora_fakes.py`) under the real `AuroraDataApiClient`. No Docker.
- **Engine integration and parity** (`packages/ontology-engine/tests/integ`) start `pgvector/pgvector:pg16` through the `docker` CLI on a random localhost port (`COA_PGVECTOR_IMAGE` overrides), apply `aurora-init.sql` as admin the way the migrator does (without the `pgrouting` line, which the image lacks), and connect a fake `rds-data` client over `asyncpg` as `coa_engine_rw`. The fake enforces a small response cap so paging is exercised. Without Docker or `asyncpg` they skip and `-rs` prints why. Parity runs one scenario through `NeptuneDBGraphStore` (over an in-memory `rdflib` dataset), `OpenSearchVectorStore` (brute-force cosine) and `dynamo_store` over moto.
- **Search integration** (`libs/common/tests/integ`) needs a Postgres you start yourself (`docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg16`) and `COA_PG_TEST_DSN` for the admin user. It applies `aurora-init.sql`, then tests the `coa_search_rw` and `coa_search_ro` roles, concurrent writers, statistics against a recount, paging and namespace-keyed labels. The tests cover concurrent writers, statistics against a recount, keyset paging, phrase recall over the cap, namespace-keyed and authoritative labels, scope deletion, the HNSW index probe, the per-role grants, `coa_set_role_password` and the `entity_label` re-key. Use `127.0.0.1` rather than `localhost` on Windows.

## 13. Follow-ups

Not done, by decision:

- **Real-cluster smoke test** of the shared client before relying on it: config through SSM names, resume from 0 ACU, `typeHint: JSON` with `CAST(... AS jsonb)`, the `::` cast through the Data API, one response over 1 MiB and one request over 4 MiB. Nothing has run on Aurora or through the real Data API yet; IAM and the custom resource are verified only by CDK synthesis tests.
- **Lambda out of the VPC.** The engine Lambda no longer needs it; the Data API does not.
- **Remove pgRouting** from `aurora-init.sql`; nothing uses it.
- **Gate the DynamoDB tables on `metadata_backend`** so the Aurora path does not create the engine table.
- **Move other packages off DynamoDB:** the namespaces and sources tables, `coa_common.idempotency` (a conditional insert with TTL; the shim's conditional-put transaction is the model), and other packages' tables. Each needs its tables in `aurora-init.sql`, grants and a role.
- **GraphRAG toolkit** chunk ingestion and retrieval, the sources stack `graph_build` / `graph_cleanup`, and the `aoss-search-proxy` Lambda still talk to AOSS. Namespace deletion therefore still deletes the AOSS GraphRAG indexes as well as clearing the Aurora scopes.
- **Relevance versus AOSS** and traversal on deep graphs are unmeasured.
- **AOSS stays for GraphRAG.** GraphRAG chunk ingestion writes to AOSS on both paths. The full-flag path keeps a scale-to-zero AOSS collection for `chunk_`, `topic_` and `statement_` indexes, and namespace deletion drops those indexes as well as clearing the Aurora scopes.
