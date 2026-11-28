# Option B spike report: GraphRAG lexical graph in Aurora (Data API) + S3 Vectors

Branch `ckg-aurora-docgraph` (base `feature/ckg-integration` @5e9d724), account 951526286029, us-east-1. graphrag-lexical-graph pinned at 3.20.0. Run from a Windows laptop against the real `ckg-dev-aurora` cluster (Data API), a new S3 vector bucket and Bedrock. Nothing was deployed; no coa-dev resource was touched.

## Verdict

**Spike passes on the criteria in findings section 8, with limits on what it proves.** The write phase ran with 4 build workers and no timeout, deadlock or retry. `topic_beam` (COA's own `_build_topic_beam_engine` config: beam 100, depth 6, all three neighbour arms) answered 16 of 16 questions end to end on Aurora + S3 Vectors. Two things the spike did not prove: parity with Neptune, and behaviour on a real 300-page contract (see "Not done").

## What was built (all in the worktree)

| Path | What |
|---|---|
| `libs/common/src/coa_common/graphrag_pg/schema.py` | Two tables: `node(id, label, tenant, value, search_str, class, props jsonb)` and `edge(type, src, dst, value, props)` unique on (type, src, dst, value). Indexes: `edge(type, dst, src)`, `node(label, tenant)`, entity `search_str` btree (`text_pattern_ops`) and trigram. |
| `.../graphrag_pg/store.py` | `AuroraGraphStore(GraphStore)` + `AuroraGraphStoreFactory` (URI `aurora-pg://?schema=lg`, registered with `GraphStoreFactory.register`). Uses COA's `AuroraDataApiClient`. |
| `scripts/spike/` | `ckg_graphrag_pg_spike.py` (schema/extract/build/query/counts), `stress_replay.py`, `read_scale.py`, `spike_embed.py`. |
| `libs/common/tests/unit/test_graphrag_pg_store.py` | 7 offline tests (query recognition, fail-closed, filter translation). Pass. |

How the store works: the toolkit sends openCypher text. Writes (`UNWIND $params AS params MERGE ...`) go through a small interpreter for exactly the grammar the builders emit (node MERGE with id key, `ON CREATE/MATCH SET v.prop = params.field`, edge MERGE with optional `{value: params.p}`, one nested UNWIND). Each batched query becomes one SQL statement: `jsonb_to_recordset` upserts with `ORDER BY id`, stub nodes `ON CONFLICT DO NOTHING`, edges `ON CONFLICT DO NOTHING`. Reads are matched by fingerprint and answered with hand-written SQL. Any other query raises `UnsupportedGraphQueryError`, which is not retried. Deadlock/serialization errors (40P01, 40001) retry up to 6 times with jitter.

## Query shapes needed so far (3.20.0, real run)

The generic write interpreter covers 17 distinct write shapes seen in the capture. Hand-written read/other handlers: 15.

| Handler | Needed by |
|---|---|
| statements by topic and source (nested map, score formula) | topic_beam |
| facts for statements | topic_beam |
| statement ids for topics | topic_beam |
| topic neighbours (entity, same-chunk, adjacent arms) | topic_beam |
| source-id-width read, write, source sample | **build** (not in findings) |
| source id collision check | **build** (not in findings) |
| version find (dynamic WHERE), ids to version x3 variants, version set | **build** with versioning (dynamic WHERE needs a filter translator; written) |
| local-entity rewrite query trees (subject complements, complement subjects, copy, delete) | **build**, runs for every fact with a non-local subject, although findings assumed it was off by default |
| `__SYS_Class__` counter writes | build (summary builder) |

Findings listed about 75 shapes and expected the build to be mostly plain MERGEs. The build also needs the 11 handlers marked above plus the summary writer, which the findings did not enumerate. None is hard, but the count shows more shapes appear as the toolkit is exercised, so the "golden test replays every code path" mitigation matters.

## Data and timing

Corpus: 12 small documents (docx and pdf, 92 to 1,255 characters each) from `fsi_commodity` onboarding s4 (6 files) and lifecycle l1 (6 files). They yield 15 chunks. Read only; nothing in the main checkout was changed.

**Extract (LLM, Sonnet 4.6 realtime, 1 worker x 2 threads):** 170.6 s for 12 documents.

**Build (4 workers, embeddings + S3 Vectors + Aurora), four runs:** 70.5 s, 67.4 s, 63.4 s, 59.3 s (the last on a non-empty graph). Almost all of it is embedding and vector writes, not Aurora.

**Aurora statements per build:** 155 to 167 Data API statements, p50 about 100 ms (about 65 ms warm), max 835 to 871 ms, summed statement time 22 to 24 s across the four workers. 0 deadlocks, 0 retries, 0 errors, 0 timeouts. The first Data API call after idle took 21.8 s (cluster resume, handled by the client). `version_find` is the slowest statement (p50 about 0.5 to 0.75 s, once per document).

| Table | Rows after one build |
|---|---|
| node | 429 total: Chunk 15, Entity 29, Fact 171, Source 12, Statement 166, Topic 31, SYS_Class 4, SYS_Config 1 |
| edge | 1,010 total: BELONGS_TO 166, EXTRACTED_FROM 15, MENTIONED_IN 197, NEXT 3, OBJECT 39, PREVIOUS 138, RELATION 39, SUBJECT 169, SUPPORTS 216, SYS_RELATION 28 |

Rebuilding the same extracted documents on a non-empty graph gave identical node and edge counts (idempotent upserts). `__SYS_Class__` counters double on every rebuild (SYS_RELATION counts 45 to 90), the same non-idempotency Neptune has.

**S3 Vectors** (bucket `ckg-dev-graphrag-vectors-951526286029`, prefix `spike`, via the toolkit's `S3VectorIndexFactory`): `chunk` 15, `topic` 31, `statement` 166 vectors, 1024 dimensions, cosine. Counts match the graph exactly.

### Scale test (synthetic, replay of the real captured writes)

The captured build writes were replayed 55 times with fresh ids (30% of entity ids shared across copies, `__SYS_Class__` shared) into a separate schema `lgstress`, through `AuroraGraphStore`. Result: **22,892 nodes and 53,876 edges (76.8k rows, 33 MB incl. indexes), the size of the low end of a 300-page contract.**

| Run | Statements | Wall time | Max statement | Deadlocks / retries / errors |
|---|---|---|---|---|
| 4 threads, 25 rows per statement (the toolkit default) | 3,300 | 102.5 s | 0.51 s | 0 / 0 / 0 |
| 8 threads, 100 rows per statement, all entities shared, re-upsert of existing rows | 1,320 | 22.3 s | 0.53 s | 0 / 0 / 0 |

Statement latency is flat at about 100 ms regardless of batch size, so throughput is set by round trips: larger `BUILD_BATCH_WRITE_SIZE` (COA sets 5 today for AOSS) cuts statement count roughly in proportion. Deadlock retry was never exercised, so it is untested under real contention.

### Read scale on that 22.9k-node graph

| Read | Size | Time |
|---|---|---|
| topic neighbours (3 arms) | 1 topic | 0.20 s |
| | 10 topics | 0.42 s |
| | 100 topics | 2.97 s |
| | 400 topics | 10.5 s |
| statements by topic and source | 2,231 statements | 3.6 s total over 4 statements |

Neighbours is the cost centre. The COA beam (width 100, depth 6) calls it with up to about 100 topics per level, so roughly 3 s per level, 18 s worst case, on a graph with 1,705 topics and hub entities. All well under the 45 s statement limit and 1 MiB response limit (the adaptive batch helper never had to halve). A 300-page document has about 2 to 4k topics, so this needs a re-test on real data and a likely index or a precomputed topic-neighbour table.

## topic_beam on 16 questions

Engine: COA `_build_topic_beam_engine` against `aurora-pg://` + `s3vectors://`. Full output in `scripts/spike/query-results.json` (worktree). Warm runs: 1.2 to 1.6 s per question (first run on a cold cache 1.8 to 5.1 s). 151 Data API statements for 16 questions (about 9.4 per question), max 313 ms. Every question returned 9 or 10 results.

| # | Question (short) | Top source | Top-1 plausible |
|---|---|---|---|
| 1 | payment term, NewCo/Shell | 001-MPTA_NewCo_Shell.docx | yes |
| 2 | late payment interest rate | 024-late-interest-invoice.pdf | partly (2.5% invoice; contract says 4%) |
| 3 | threshold, legal-reviewed terms | 012-mpta-legal-review.pdf | yes |
| 4 | governing law, Glencore ISDA | 011-isda-schedule-round-1.docx | yes (right doc, top statement off) |
| 5 | cross default threshold | 012-isda-schedule-round-2.docx | yes (25,000,000) |
| 6 | approved Glencore limit | 008-glencore-ltd-credit-memo.pdf | yes (20,000,000) |
| 7 | netting opinion qualifications | 016-glencore-netting-opinion.pdf | yes |
| 8 | margin call amount | 025-margin-call-2026-10-30.pdf | yes (2,312,400) |
| 9 | minimum transfer amount | 001-MPTA_NewCo_Shell.docx | yes |
| 10 | notice for end/renewal | 002-MPTA_NewCo_Shell.docx | plausible |
| 11 | product and quantity, NC-LON-26-0201 | 020-...confirmation.pdf | yes |
| 12 | buyer and seller | 020-...confirmation.pdf | yes |
| 13 | credit approval conditions | 008-glencore-ltd-credit-memo.pdf | yes |
| 14 | close-out netting calculation | 011-isda-schedule-round-1.docx | yes |
| 15 | late interest invoiced | 024-late-interest-invoice.pdf | yes |
| 16 | margin call valuation time | 025-margin-call-2026-10-30.pdf | yes |

"Plausible" is my judgement from the document text, not a Neptune comparison.

## Problems found

1. **`TopicBeamSearch` 3.20.0 does not work with S3 Vectors.** It seeds with `topic_index.client.query(...)`, an OpenSearch client call (`topic_beam_search.py:110`). `S3VectorIndex.client` is the boto3 `s3vectors` client, which has no `query`. Every question failed with `AttributeError` until the spike patched `get_start_node_ids` to use the backend-neutral `VectorIndex.top_k`. COA needs a `TopicBeamSearch` subclass in `strategies._build_topic_beam_engine` for the S3 path. It is the only retriever in 3.20.0 that touches `.client`.
2. **More Cypher shapes than findings listed** (table above): source-id-width, source collision, local-entity query trees, version manager with a dynamic metadata WHERE (needs a translator; written, supports `= <> < <= > >= CONTAINS STARTS WITH ENDS WITH`, AND/OR/NOT, bound parameters only).
3. **COA's Cohere embedding adapter embeds one text per `InvokeModel`** (it does not override `_get_text_embeddings`, so LlamaIndex loops). With 4 workers the first build died on `ThrottlingException` at 20 RPM. Spike used a batching adapter with a long throttle backoff (`scripts/spike/spike_embed.py`). Worth fixing in COA regardless of this option. Based on code reading plus the observed throttle; I did not count calls.
4. **Data API cost is round trips**, about 100 ms each. Raise `BUILD_BATCH_WRITE_SIZE` on this path (25 is the toolkit default, COA uses 5).
5. **Topic-neighbour read cost grows with topics per call** (table above).
6. **`__SYS_Class__` counters** are hot, non-idempotent rows (same as Neptune). One statement per batch after in-Python aggregation; no contention seen.
7. Laptop-only issues, not production: `FileBasedDocs` needs a sanitizer for `:` on Windows; nltk's path-security check rejects uv hard-linked files (fixed with a copied `NLTK_DATA`).
8. S3 Vectors index names come out as `<prefix>.chunk-<tenant>`; worked, but confirm the prefix character rules before choosing a production prefix.

## Not done

- **Neptune parity (overlap@10).** coa-dev was not touched and no Neptune ingest of these documents exists, so there is no comparison. Top-1 plausibility above is the only quality signal.
- **A real 300-page document.** Scale numbers come from a replay of small-document writes, not from a real contract. Entity cardinality (29 real, 1,163 in the replay) is far below the 4 to 8k estimate, so hub-entity cost in the neighbour query is under-tested.
- Real deadlock contention (none occurred), `chunk_based_semantic` and the other six strategies, delete/cleanup queries, ontology induction reads, COA serve tools (9 openCypher templates), `phrase_search`, CDK/infra (S3 vector bucket, `s3vectors` VPC endpoint, IAM), Aurora full-text index.
- Aurora used the admin secret for the spike. Production should use a scoped role.

## Recommendation

Continue with Option B. The write path is cheap and reliable at the scale tested, the S3 Vectors indexes match the graph exactly, and the one retrieval strategy tested works end to end. Before committing to all strategies:

1. Run a real 300-page contract (extraction at about 10 RPM takes hours; use batch inference or ask for a quota raise) to measure entity cardinality and the neighbour query.
2. Add the `TopicBeamSearch` subclass and the topic-neighbour index/table.
3. Add a golden test that replays every toolkit code path against a recording store, because a normal build already needed 12 shapes (11 handlers and the summary writer) the findings missed.

## Open question for you

Stop after `topic_beam` + `chunk_based_semantic`, or continue to all strategies COA enables? Each extra strategy adds read shapes (entity_based needs a recursive path query; entity_context, entity_network, topic_based and traversal need the entity-lookup and 2 to 4 hop queries). Estimated findings effort: about 22 to 28 engineer-days for the reduced scope, 37 to 46 for all.

## Resources left in AWS (ckg-dev only)

- S3 vector bucket `ckg-dev-graphrag-vectors-951526286029` with indexes `spike.chunk-/topic-/statement-5b1c0de5b1c0de5b1c0de5b1c`.
- Aurora (`postgres` database) schemas `lg` (the 12-document graph) and `lgstress` (76.8k synthetic rows). Drop `lgstress` when no longer needed.
