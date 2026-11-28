# Aurora PostgreSQL + pgvector Backend
How the ontology engine stores graphs, embeddings and its own metadata in
Aurora PostgreSQL through the RDS Data API, how that maps onto the Neptune and
OpenSearch backends, and what is still open.
Audience: a developer who knows SQL and AWS and has read the backend section of
the package README. Terms such as named graph and T-Box are explained in
[`ontology-data-model.md`](ontology-data-model.md).
Primary sources for everything below:
- `src/coa_ontology/stores/aurora/schema.py` — DDL and `ensure_schema`
- `src/coa_ontology/stores/aurora/graph.py` — `AuroraGraphStore`
- `src/coa_ontology/stores/aurora/vector.py` — `AuroraVectorStore`
- `src/coa_ontology/aurora_metadata.py` — DynamoDB shim for the engine table
- `src/coa_ontology/stores/factory.py` — backend selection
- `libs/common/src/coa_common/aurora_data_api/` — shared Data API layer
- `.agents/tasks/aurora-pgvector/plan.md`, `part2-results.md` — decisions and recorded parity gaps
---
## 1. Purpose
One Aurora cluster holds the three things the engine persists: the RDF graph,
the embeddings, and the DynamoDB single-table metadata (registry, jobs, locks,
proposals). The connection is the RDS Data API (`boto3` `rds-data`). There is no
Postgres driver, no connection pool, and no VPC, endpoint or NAT requirement on
the engine's side of the connection.
## 2. Enabling it
| Variable | Effect |
|---|---|
| `WORKBENCH_BACKEND=aurora_pgvector` | `build_stores()` returns `(AuroraGraphStore, AuroraVectorStore)`. |
| `METADATA_BACKEND=aurora` or `dynamodb` | Selects where `dynamo_store` keeps its items. When unset it is `aurora` only if `WORKBENCH_BACKEND=aurora_pgvector`. Any other value raises `ValueError`. |
The default path (`na_only`, `opensearch_neptune`, DynamoDB) is unchanged.
## 3. Environment variables
| Variable | Meaning |
|---|---|
| `AURORA_CLUSTER_ARN` | Cluster ARN (preferred). `AURORA_RESOURCE_ARN` is accepted as an alias. |
| `AURORA_CLUSTER_ENDPOINT` | Used only when no ARN is set and the value starts with `/`. It is then read as an SSM name and the sibling parameter `<same prefix>/cluster-arn` supplies the ARN. |
| `AURORA_SECRET_ARN` | Secrets Manager ARN used as is when it starts with `arn:`. Otherwise it is read as an SSM parameter name. The Data API reads the secret itself; the engine never sees credentials. |
| `AURORA_DATABASE` | Database name. Default `postgres` (the CDK cluster has no default database). |
| `AURORA_VECTOR_DIMENSIONS` | Embedding width. Falls back to `OSS_DIMENSIONS`, then 1024. Range 1 to 2000 (HNSW limit). |
| `AURORA_RESUME_TIMEOUT_SECONDS` | Retry budget for a paused cluster. Default 45. |
| `NDB_GRAPH_URI_BASE` | Reused from the Neptune store: the named-graph IRI base. |
SSM reads are lazy, cached per process, and need `ssm:GetParameter` on the
parameter. When the grant is missing the error names it. Today the CDK stack
passes SSM names for the endpoint and secret and grants `ssm:GetParameter` only
for `bedrock/guardrail-id`, so either set a literal cluster ARN and secret ARN
or add the grant (see section 11).
## 4. Schema
The engine creates its own tables (`stores/aurora/schema.py`). `aurora-init.sql`
creates extensions and the `ontology_engine` table but has no table for triples
or ontology embeddings, and the package cannot ship `.sql` data files, so the
DDL lives in Python strings. `ensure_schema()` runs once per client. When the
schema is current it costs one round trip. Otherwise it applies idempotent
`CREATE ... IF NOT EXISTS` statements in one transaction under
`pg_advisory_xact_lock`, so concurrent cold starts do not race.
| Table | Content | Indexes |
|---|---|---|
| `coa_triple` | One row per quad: `namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype`. | Unique `(graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype)` gives RDF set semantics. Covering `(namespace, s, p) INCLUDE (o, graph_uri)` and `(namespace, o, p) INCLUDE (s, graph_uri)`, both partial on `o_kind='iri'`. `(namespace, p, s)`. Trigram GIN on `lower(o)` for literals. |
| `coa_embedding` | One row per `(namespace, ontology_id, entity_uri, embedding_type, model_id)` with a `vector(N)` column. | Unique on that key, `(namespace, entity_uri)`, HNSW `vector_cosine_ops`. |
| `coa_schema_meta` | Schema version. | Primary key. |
| `ontology_engine` | `(pk, sk, attributes jsonb)`, the DynamoDB mirror from `aurora-init.sql`, used as is. | Adds `pk text_pattern_ops` for `begins_with`. |
`md5(o)` in the unique key keeps long literals out of the btree (entries over
about 2.7 KB fail). A database whose `coa_embedding` column has another width
than the configured one raises on first use; the column cannot be altered in
place.
## 5. Graph mapping
- **Named graphs.** Each ontology is the graph `{base}/{ns}/{quote(ontology_id)}`, built by the Neptune store's `_ontology_graph_uri` helper, stored in `graph_uri`. The `namespace` column is the scope Neptune expresses as the `{base}/{ns}/` prefix. Every read filters on `namespace` first.
- **Turtle in.** `load_turtle` parses with `rdflib` and bulk-inserts rows with `ON CONFLICT DO NOTHING`.
- **Turtle out.** `get_graph_turtle` reads rows paged by `id` and serialises with `rdflib`.
- **Classes and properties.** `store_class`, `store_property` and the batch variants take their triples from the Neptune store's own builders, so both backends store identical triples.
- **Search, overview, schema.** `search_entities`, `count_entities`, `get_vertex`, `get_ontology_overview` and `get_namespace_schema` are SQL statements over `coa_triple`. Label, kind and language choices reuse the Neptune helpers (`_literal_candidate`, `_kind_from_type`, `LabelPreference`), so they match. Search orders with `COLLATE "C"` to match SPARQL codepoint order.
- **Sample overview.** The subtree sizes that rank seed roots come from one recursive `traverse` statement. The 50-descendant breadth-first fill stays the Neptune algorithm in Python, so the seed is identical.
## 6. Traversal
`AuroraGraphStore.traverse(start_uris, *, predicates, direction, max_depth, namespace, ontology_id, limit, return_paths=False, node_types=None)` is a store-specific method, not part of the `GraphStore` Protocol.
- One `WITH RECURSIVE` statement per call. `max_depth` is mandatory.
- Plain reachability uses `UNION`, which removes revisited nodes and ends cycles. With `return_paths` the statement keeps a path array, uses `UNION ALL` and excludes nodes already on the path.
- `node_types` restricts the walk to nodes typed so in the edge's graph.
- The covering indexes in section 4 serve both directions.
- **pgRouting is not used.** `aurora-init.sql` creates the extension; this store does not call it. Recursive CTEs need no extension and are enough for the bounded hierarchies the engine walks.
## 7. Data API limits
| Limit | Handling |
|---|---|
| 1 MiB response | `formatRecordsAs=JSON`. `ResponseTooLarge` is raised by the client. `query_paged` pages by keyset and halves the page on that error. Vector reads fall back to ranking first and fetching vectors in pages of 25. |
| 4 MiB request | Bulk writes send one JSON string parameter per chunk (about 1.5 MiB) and expand it with `jsonb_to_recordset`. `batch_execute` chunks at 500 parameter sets and 2 MiB. |
| 45 s statement | Large deletes run in bounded batches. |
| No array parameters | Lists go as a JSON string and expand with `jsonb_array_elements_text`. Vectors go as text, `CAST(CAST(:v AS text) AS vector)`, and read back as `embedding::text`. |
| Paused cluster (min ACU 0) | `DatabaseResumingException` and resume-style errors retry with exponential backoff and jitter within `AURORA_RESUME_TIMEOUT_SECONDS`. |
## 8. DynamoDB shim
`dynamo_store.py` is written against boto3 `Table` calls with string
expressions. Rather than rewrite about 60 functions, `_get_table()` and
`_get_dao()` return `AuroraTable` and `AuroraDao` when the selector says
aurora. Items live in `ontology_engine(pk, sk, attributes)`.
- Unconditional get, put and delete are one statement.
- Conditional put, update and delete (induction lock, accept-state guards) run in one transaction: advisory lock on `(pk, sk)`, `SELECT ... FOR UPDATE`, condition check, write. A failed condition raises a botocore `ClientError` with code `ConditionalCheckFailedException`.
- Query and scan push `begins_with(PK, ...)` and `SK =` to SQL, filter the rest in Python, and honour `Limit`, `ExclusiveStartKey` and `LastEvaluatedKey` by keyset on `(pk, sk)`.
- Numbers return as `Decimal`; sets and `None` round-trip.
- **Expression coverage.** Conditions: `AND`, `OR`, `NOT`, parentheses, `= <> < <= > >=`, `attribute_exists`, `attribute_not_exists`, `begins_with`, `contains`. Updates: `SET` (with `if_not_exists`, `list_append`, arithmetic), `REMOVE`, `ADD`. `size()`, `IN`, `BETWEEN`, nested paths, `DELETE` and `attribute_type` raise `NotImplementedError`.
- The API proxy health probe runs `SELECT 1` through the Data API on this path and reports it under the existing `dynamodb` key.
## 9. Decisions
1. **Connection:** RDS Data API through `boto3` `rds-data`; no driver, no VPC, endpoint or NAT requirement. The shared layer is `coa_common.aurora_data_api`.
2. **Config values may be SSM names:** see section 3.
3. **RDF as one quad table** `coa_triple`, with Neptune helpers imported rather than copied.
4. **Traversal by recursive CTE only**, no pgRouting.
5. **Vectors** in `coa_embedding`, upsert on the natural key, cosine HNSW. `search_nearest` sets `hnsw.ef_search = max(40, top_k)` in a transaction when `top_k > 40`, and `hnsw.iterative_scan = relaxed_order` only for filtered queries on pgvector 0.8 or later.
6. **App-owned DDL** instead of editing `aurora-init.sql`.
7. **The engine's own DynamoDB table moves** behind a shim; the selector is `METADATA_BACKEND`.
8. **Other DynamoDB use stays** (section 11).
9. **Tests** run against a fake `rds-data` client in unit tests and against a real pgvector container in integration tests, with parity against the existing backends.
Further choices recorded in `part2-results.md`: `get_ontology_overview` and `count_classes_and_properties` use the call's `namespace`; `get_vertex` uses `namespace or bound or default`; `list_embeddings_for_ontology` without `limit` is capped at 10,000 rows like OpenSearch; the module lock in `aurora_metadata` is an `RLock`.
## 10. Behaviour differences
- **Upsert vs append.** `store_embeddings` replaces a row with the same natural key. OpenSearch appends duplicates. Callers that delete first are unaffected.
- **Cosine score.** `search_nearest` returns raw cosine in [-1, 1], as `base.py` requires. OpenSearch returns `(1 + cosine) / 2`; parity compares ranking and converts.
- **Search with no match.** Aurora returns an empty page and count 0. The rdflib-backed Neptune reference returns the whole namespace because of an unbound `GROUP BY` row; real Neptune is guarded against it elsewhere.
- **Several parents.** Neptune's `parentClass` takes an arbitrary parent; Aurora takes the first stored.
- **Grounding.** Aurora folds one row per predicate in priority order (`coa:groundedTo`, `skos:exactMatch`, `skos:closeMatch`), which gives a deterministic result equal to Neptune's preference.
- **Blank nodes.** Ids are random per parse on every backend; parity masks them.
- **Metadata scan order.** Aurora scans in `(pk, sk)` order under `C` collation; moto's order is undefined, so parity sorts. Moto raises a subclass of `ClientError` where Aurora raises `ClientError`; parity compares the error code.
## 11. Tests
From the repo root, with `PYTHONUTF8=1`:
    uv run --frozen --all-packages pytest packages/ontology-engine -m unit -q
    uv run --frozen --all-packages pytest libs/common/tests/unit -m unit -q
    uv run --frozen --all-packages pytest packages/ontology-engine/tests/integ -m integ -rs
- **Unit** tests use a fake `rds-data` client (`tests/unit/aurora_fakes.py`) that records SQL and parameters. No Docker.
- **Integration and parity** tests (`tests/integ/`, marker `integ`) start `pgvector/pgvector:pg16` through the `docker` CLI on a random localhost port and remove it afterwards. Set `COA_PGVECTOR_IMAGE` to use another image. A fake `rds-data` client over `asyncpg` runs the statements and enforces a small response cap so paging is exercised. When the Docker CLI or daemon is missing, or `asyncpg` is not installed, those tests skip and `-rs` prints the reason.
- **Parity** runs one scenario through `NeptuneDBGraphStore` (over an in-memory `rdflib` dataset), `OpenSearchVectorStore` (brute-force cosine) and `dynamo_store` over moto, then compares normalised results.
On Windows, `import coa_common` needs a `resource` module (`bedrock_metrics.py` imports it). Put a stub on `PYTHONPATH`. See `part2-results.md` for the baseline failures that exist without this work.
## 12. Reusing the Data API layer
`coa_common.aurora_data_api` (`AuroraConfig`, `DataApiClient.execute`, `batch_execute`, `transaction`, `query_paged`) has no engine dependency. To move another package:
- **context-manager, metric-service, control-plane, sources:** replace their DynamoDB or Postgres access with a `DataApiClient`, define their tables in their own idempotent DDL, and give the task role `rds-data:*` and `secretsmanager:GetSecretValue`.
- **`libs/common` idempotency (`coa_common.idempotency`):** needs a conditional insert with a TTL column. The shim's conditional-put transaction is the model.
- **Namespaces and sources tables** read by `induce_catalog.py`, `namespace_language.py` and `graph_source_resolver.py`: owned by other packages; they need their own port first.
## 13. Follow-ups
Not done, by decision:
- **Infra environment and IAM.** Set `AURORA_CLUSTER_ARN` and a real `AURORA_SECRET_ARN` in the Lambda env, or grant `ssm:GetParameter` on `<prefix>/aurora/*`. Add `METADATA_BACKEND`. The engine task needs `rds-data` permissions.
- **VPC/NAT.** Move the Lambda out of the VPC; the Data API does not need it.
- **pgRouting.** Remove the extension from `aurora-init.sql`; nothing uses it.
- **DynamoDB tables.** Make the stack's DynamoDB tables depend on `metadata_backend`.
- **Other DynamoDB use:** the namespaces table, the sources table, `coa_common.idempotency`, and other packages' tables.
- **Hybrid and lexical search** on pgvector (the `aurora-search-layer` branch).
- **Traversal benchmarking** for deep graphs in `tests/benchmark`.
