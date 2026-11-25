# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Aurora PostgreSQL search index: pgvector + tsvector recall + Python BM25 + trigram, fused by RRF.

Tables come from ``aurora-init.sql``. ``namespace`` holds the AOSS-equivalent index name
(the scope). All values are bound as ``:name`` parameters. The only interpolated token is
the integer vector dimension, validated to 1..16000. Statements avoid the ``::`` cast
syntax so the Data API placeholder parser cannot misread them. Read methods never create
indexes or tables.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from coa_common.aurora_search.bm25 import Candidate, CorpusStats, rank_bm25
from coa_common.aurora_search.data_api import AuroraDataApiClient, AuroraQueryError
from coa_common.aurora_search.fusion import SearchMode, reciprocal_rank_fusion
from coa_common.aurora_search.text import (
    build_or_prefix_tsquery,
    build_phrase_query,
    parse_tsvector_text,
    tokenize,
)

logger = logging.getLogger(__name__)

DOC_GROUP_SIZE = 50
DELETE_BATCH_SIZE = 1000
SCOPE_DELETE_CHUNK = 5000
MIN_DIMENSIONS = 1
MAX_DIMENSIONS = 16000

# (cluster_arn, dimensions) pairs whose HNSW index has been ensured in this process.
_ENSURED_INDEXES: set[tuple[str, int]] = set()

_ENTITY_KEY_SQL = "COALESCE(NULLIF(metadata ->> 'entity_uri', ''), doc_id)"


@dataclass
class SearchDoc:
    """A document to index. The identity fields form the idempotent ``doc_id``."""

    body: str
    doc_kind: str = "doc"
    entity_uri: str = ""
    embedding_type: str = ""
    model_id: str = ""
    ontology_id: str = ""
    embedding: Sequence[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    label: str | None = None
    aliases: Sequence[str] = ()


@dataclass
class SearchHit:
    """One search result. ``score`` is cosine similarity, BM25 or the RRF score depending on the mode."""

    key: str
    doc_id: str
    score: float
    doc_kind: str = "doc"
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ranks: dict[str, int] = field(default_factory=dict)
    matched_phrases: list[str] = field(default_factory=list)


@dataclass
class FuzzyHit:
    """A trigram match on an entity label."""

    entity_id: str
    label: str
    score: float


def _validate_dimensions(dimensions: Any) -> int:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError(f"dimensions must be an integer, got {dimensions!r}")
    if not MIN_DIMENSIONS <= dimensions <= MAX_DIMENSIONS:
        raise ValueError(f"dimensions must be between {MIN_DIMENSIONS} and {MAX_DIMENSIONS}, got {dimensions}")
    return dimensions


def doc_id_for(scope: str, doc: SearchDoc) -> str:
    """Stable id: sha256 of scope|doc_kind|entity_uri|embedding_type|model_id|ontology_id."""
    raw = "|".join([scope, doc.doc_kind, doc.entity_uri, doc.embedding_type, doc.model_id, doc.ontology_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _obj(value: Any) -> Any:
    """Data API returns jsonb columns either parsed or as a JSON string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _meta(value: Any) -> dict[str, Any]:
    parsed = _obj(value)
    return parsed if isinstance(parsed, dict) else {}


def _terms(value: Any) -> set[str]:
    parsed = _obj(value)
    return {str(t) for t in parsed} if isinstance(parsed, list) else set()


def _fit_vector(vector: Sequence[float], dimensions: int) -> str:
    values = [float(x) for x in vector][:dimensions]
    values.extend([0.0] * (dimensions - len(values)))
    return "[" + ",".join(format(x, ".8g") for x in values) + "]"


def _build_filters(filters: dict[str, Any] | None, prefix: str = "f") -> tuple[str, dict[str, Any]]:
    """Translate a filter dict into ``AND ...`` SQL and bound parameters.

    ``ontology_ids``: list, OR over ``metadata @> {"ontology_id": x}``.
    ``require_mapped``: true requires ``metadata`` to have ``data_source_id``.
    ``exclude_metadata``: dict; rows whose ``metadata`` contains it are excluded.
    ``doc_kind``: str or list matched against the column.
    Any other key: a list is an OR over containment of ``{key: v}``; a scalar is one containment.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    n = 0

    def add(value: Any) -> str:
        nonlocal n
        name = f"{prefix}{n}"
        n += 1
        params[name] = value
        return name

    for key, value in (filters or {}).items():
        if value is None or value == [] or value is False:
            continue
        if key == "require_mapped":
            clauses.append("jsonb_exists(metadata, 'data_source_id')")
        elif key == "exclude_metadata":
            clauses.append(f"NOT (metadata @> CAST(:{add(value)} AS jsonb))")
        elif key == "doc_kind":
            kinds = value if isinstance(value, list) else [value]
            clauses.append(
                "doc_kind IN (SELECT jsonb_array_elements_text(CAST(:" + add([str(k) for k in kinds]) + " AS jsonb)))"
            )
        else:
            meta_key = "ontology_id" if key == "ontology_ids" else key
            values = value if isinstance(value, list) else [value]
            ors = [f"metadata @> CAST(:{add({meta_key: v})} AS jsonb)" for v in values]
            clauses.append("(" + " OR ".join(ors) + ")")
    sql = "".join(f" AND {c}" for c in clauses)
    return sql, params


class AuroraSearchIndex:
    """Ingest and search documents stored in the Aurora ``search_doc`` tables."""

    def __init__(self, client: AuroraDataApiClient, dimensions: int) -> None:
        """Bind to a Data API client and a vector dimension (1..16000)."""
        self._client = client
        self._dimensions = _validate_dimensions(dimensions)

    @property
    def dimensions(self) -> int:
        """The configured embedding dimension."""
        return self._dimensions

    # ── write path ────────────────────────────────────────────────────────

    def ensure_vector_index(self) -> None:
        """Create the HNSW index for this dimension. Runs once per process per dimension."""
        dim = _validate_dimensions(self._dimensions)
        cache_key = (getattr(self._client, "cluster_arn", ""), dim)
        if cache_key in _ENSURED_INDEXES:
            return
        sql = (
            f"CREATE INDEX IF NOT EXISTS search_doc_embedding_hnsw_{int(dim)} ON search_doc "
            f"USING hnsw (CAST(embedding AS vector({int(dim)})) vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        )
        try:
            self._client.execute(sql)
        except AuroraQueryError as exc:
            if "already exists" not in str(exc).lower():
                raise
        _ENSURED_INDEXES.add(cache_key)

    def upsert_docs(self, scope: str, docs: Sequence[SearchDoc]) -> int:
        """Insert or update documents idempotently and keep term and corpus statistics current."""
        unique: dict[str, SearchDoc] = {}
        for doc in docs:
            unique[doc_id_for(scope, doc)] = doc
        if not unique:
            return 0
        if any(d.embedding is not None for d in unique.values()):
            self.ensure_vector_index()
        items = list(unique.items())
        for start in range(0, len(items), DOC_GROUP_SIZE):
            self._upsert_group(scope, items[start : start + DOC_GROUP_SIZE])
        return len(items)

    def _upsert_group(self, scope: str, group: list[tuple[str, SearchDoc]]) -> None:
        c = self._client
        with c.transaction():
            existing_rows = c.execute(
                "SELECT doc_id, to_jsonb(tsvector_to_array(tsv)) AS terms, doc_len FROM search_doc "
                "WHERE doc_id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
                {"ids": [doc_id for doc_id, _ in group]},
            )
            existing = {r["doc_id"]: (_terms(r["terms"]), int(r["doc_len"])) for r in existing_rows}
            tsv_rows = c.execute(
                "SELECT CAST(e.ord AS integer) AS ord, CAST(to_tsvector('english', e.val) AS text) AS tsv_text "
                "FROM jsonb_array_elements_text(CAST(:bodies AS jsonb)) WITH ORDINALITY AS e(val, ord)",
                {"bodies": [doc.body for _, doc in group]},
            )
            tsv_by_ord = {int(r["ord"]): parse_tsvector_text(r["tsv_text"] or "") for r in tsv_rows}

            term_delta: Counter[str] = Counter()
            count_delta = 0
            len_delta = 0
            labels: dict[tuple[str, str], str] = {}
            for i, (doc_id, doc) in enumerate(group, start=1):
                tf = tsv_by_ord.get(i, {})
                new_terms = set(tf)
                new_len = sum(tf.values())
                old_terms, old_len = existing.get(doc_id, (set(), 0))
                for term in new_terms - old_terms:
                    term_delta[term] += 1
                for term in old_terms - new_terms:
                    term_delta[term] -= 1
                if doc_id not in existing:
                    count_delta += 1
                len_delta += new_len - old_len

                metadata = dict(doc.metadata)
                for key, value in (
                    ("entity_uri", doc.entity_uri),
                    ("embedding_type", doc.embedding_type),
                    ("model_id", doc.model_id),
                    ("ontology_id", doc.ontology_id),
                ):
                    if value:
                        metadata[key] = value
                embedding = None if doc.embedding is None else _fit_vector(doc.embedding, self._dimensions)
                c.execute(
                    "INSERT INTO search_doc (doc_id, namespace, body, doc_len, tsv, embedding, doc_kind, metadata) "
                    "VALUES (:doc_id, :ns, :body, :doc_len, to_tsvector('english', :body), "
                    "CAST(:embedding AS vector), :doc_kind, CAST(:metadata AS jsonb)) "
                    "ON CONFLICT (doc_id) DO UPDATE SET namespace = EXCLUDED.namespace, body = EXCLUDED.body, "
                    "doc_len = EXCLUDED.doc_len, tsv = EXCLUDED.tsv, embedding = EXCLUDED.embedding, "
                    "doc_kind = EXCLUDED.doc_kind, metadata = EXCLUDED.metadata",
                    {
                        "doc_id": doc_id,
                        "ns": scope,
                        "body": doc.body,
                        "doc_len": new_len,
                        "embedding": embedding,
                        "doc_kind": doc.doc_kind,
                        "metadata": metadata,
                    },
                )
                entity_id = doc.entity_uri or doc_id
                if doc.label:
                    labels[(entity_id, doc.label)] = "label"
                for alias in doc.aliases:
                    if alias:
                        labels.setdefault((entity_id, alias), "alias")

            self._apply_stats(scope, term_delta, count_delta, len_delta)
            if labels:
                c.batch_execute(
                    "INSERT INTO entity_label (entity_id, namespace, label, kind) "
                    "VALUES (:entity_id, :ns, :label, :kind) "
                    "ON CONFLICT (entity_id, label) DO UPDATE SET namespace = EXCLUDED.namespace, kind = EXCLUDED.kind",
                    [
                        {"entity_id": eid, "ns": scope, "label": label, "kind": kind}
                        for (eid, label), kind in labels.items()
                    ],
                )

    def _apply_stats(self, scope: str, term_delta: Counter[str], count_delta: int, len_delta: int) -> None:
        """Apply net term and corpus deltas. Call inside a transaction."""
        c = self._client
        sets = [{"ns": scope, "term": t, "delta": d} for t, d in term_delta.items() if d != 0]
        if sets:
            c.batch_execute(
                "INSERT INTO search_term_stats (namespace, term, doc_freq) VALUES (:ns, :term, :delta) "
                "ON CONFLICT (namespace, term) DO UPDATE SET doc_freq = search_term_stats.doc_freq + EXCLUDED.doc_freq",
                sets,
            )
        if any(d < 0 for d in term_delta.values()):
            c.execute("DELETE FROM search_term_stats WHERE namespace = :ns AND doc_freq <= 0", {"ns": scope})
        if count_delta or len_delta:
            c.execute(
                "INSERT INTO search_corpus_stats AS s (namespace, doc_count, avg_doc_len) "
                "VALUES (:ns, :dc, CASE WHEN :dc <= 0 THEN 0 ELSE CAST(:dl AS double precision) / :dc END) "
                "ON CONFLICT (namespace) DO UPDATE SET "
                "doc_count = s.doc_count + :dc, "
                "avg_doc_len = CASE WHEN s.doc_count + :dc <= 0 THEN 0 "
                "ELSE (s.avg_doc_len * s.doc_count + :dl) / (s.doc_count + :dc) END",
                {"ns": scope, "dc": count_delta, "dl": len_delta},
            )

    def delete_docs(
        self,
        scope: str,
        filter_metadata: dict[str, Any] | None = None,
        doc_ids: Sequence[str] | None = None,
    ) -> int:
        """Delete documents by metadata containment and/or id, and reverse their statistics."""
        if not filter_metadata and not doc_ids:
            raise ValueError("delete_docs needs filter_metadata or doc_ids; use delete_scope to clear a scope")
        where = "namespace = :ns"
        params: dict[str, Any] = {"ns": scope}
        if filter_metadata:
            where += " AND metadata @> CAST(:fm AS jsonb)"
            params["fm"] = filter_metadata
        if doc_ids:
            where += " AND doc_id IN (SELECT jsonb_array_elements_text(CAST(:dids AS jsonb)))"
            params["dids"] = list(doc_ids)
        total = 0
        c = self._client
        while True:
            with c.transaction():
                rows = c.execute(
                    "SELECT doc_id, to_jsonb(tsvector_to_array(tsv)) AS terms, doc_len, "
                    f"{_ENTITY_KEY_SQL} AS entity_key FROM search_doc WHERE {where} LIMIT :lim",
                    {**params, "lim": DELETE_BATCH_SIZE},
                )
                if not rows:
                    return total
                ids = [r["doc_id"] for r in rows]
                c.execute(
                    "DELETE FROM search_doc WHERE doc_id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
                    {"ids": ids},
                )
                term_delta: Counter[str] = Counter()
                len_delta = 0
                for r in rows:
                    for term in _terms(r["terms"]):
                        term_delta[term] -= 1
                    len_delta -= int(r["doc_len"])
                self._apply_stats(scope, term_delta, -len(rows), len_delta)
                c.execute(
                    "DELETE FROM entity_label WHERE namespace = :ns "
                    "AND entity_id IN (SELECT jsonb_array_elements_text(CAST(:eids AS jsonb))) "
                    "AND NOT EXISTS (SELECT 1 FROM search_doc d WHERE d.namespace = :ns "
                    "AND COALESCE(NULLIF(d.metadata ->> 'entity_uri', ''), d.doc_id) = entity_label.entity_id)",
                    {"ns": scope, "eids": sorted({r["entity_key"] for r in rows})},
                )
            total += len(rows)

    def delete_scope(self, scope: str) -> int:
        """Delete every document, statistic and label of a scope. Idempotent."""
        c = self._client
        total = 0
        while True:
            rows = c.execute(
                "WITH d AS (DELETE FROM search_doc WHERE ctid IN "
                "(SELECT ctid FROM search_doc WHERE namespace = :ns LIMIT :lim) RETURNING 1) "
                "SELECT count(*) AS n FROM d",
                {"ns": scope, "lim": SCOPE_DELETE_CHUNK},
            )
            deleted = int(rows[0]["n"]) if rows else 0
            if deleted <= 0:
                break
            total += deleted
        c.execute("DELETE FROM search_term_stats WHERE namespace = :ns", {"ns": scope})
        c.execute("DELETE FROM search_corpus_stats WHERE namespace = :ns", {"ns": scope})
        c.execute("DELETE FROM entity_label WHERE namespace = :ns", {"ns": scope})
        return total

    # ── read path ─────────────────────────────────────────────────────────

    @staticmethod
    def _hit(row: dict[str, Any], score: float) -> SearchHit:
        metadata = _meta(row.get("metadata"))
        doc_id = str(row["doc_id"])
        return SearchHit(
            key=str(metadata.get("entity_uri") or doc_id),
            doc_id=doc_id,
            score=score,
            doc_kind=str(row.get("doc_kind") or "doc"),
            body=str(row.get("body") or ""),
            metadata=metadata,
        )

    def vector_search(
        self,
        scope: str,
        vector: Sequence[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Cosine nearest neighbours; ``score`` is cosine similarity in [-1, 1]."""
        dim = _validate_dimensions(self._dimensions)
        filter_sql, filter_params = _build_filters(filters)
        distance = f"CAST(embedding AS vector({dim})) <=> CAST(:q AS vector({dim}))"
        sql = (
            f"SELECT doc_id, doc_kind, body, metadata, 1 - ({distance}) AS score FROM search_doc "
            f"WHERE namespace = :ns AND embedding IS NOT NULL{filter_sql} ORDER BY {distance} LIMIT :k"
        )
        params = {"ns": scope, "q": _fit_vector(vector, dim), "k": int(top_k), **filter_params}
        if filter_sql:
            with self._client.transaction():
                self._client.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
                rows = self._client.execute(sql, params)
        else:
            rows = self._client.execute(sql, params)
        return [self._hit(r, float(r["score"])) for r in rows]

    def keyword_search(
        self,
        scope: str,
        query: str,
        top_k: int,
        recall: int = 200,
        phrases: Sequence[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Prefix OR recall through the GIN index, then BM25 over the candidates."""
        tsquery = build_or_prefix_tsquery(query)
        phrase_list = [build_phrase_query(p) for p in (phrases or []) if build_phrase_query(p)]
        if not tsquery and not phrase_list:
            return []
        c = self._client
        lexeme_rows = c.execute(
            "SELECT unnest(tsvector_to_array(to_tsvector('english', :q))) AS lexeme",
            {"q": " ".join([query, *phrase_list])},
        )
        query_terms = [f"{r['lexeme']}:*" for r in lexeme_rows]

        filter_sql, params = _build_filters(filters)
        params.update({"ns": scope, "q": tsquery, "recall": int(recall)})
        phrase_cols = ""
        match_clauses = ["tsv @@ to_tsquery('english', :q)"]
        for i, phrase in enumerate(phrase_list):
            params[f"ph{i}"] = phrase
            phrase_cols += f", (tsv @@ phraseto_tsquery('english', :ph{i})) AS ph{i}"
            match_clauses.append(f"tsv @@ phraseto_tsquery('english', :ph{i})")
        rows = c.execute(
            "SELECT doc_id, doc_kind, doc_len, CAST(tsv AS text) AS tsv_text, metadata, body, "
            f"ts_rank(tsv, to_tsquery('english', :q)) AS rank{phrase_cols} FROM search_doc "
            f"WHERE namespace = :ns AND ({' OR '.join(match_clauses)}){filter_sql} "
            "ORDER BY rank DESC, doc_id LIMIT :recall",
            params,
        )
        if not rows:
            return []

        candidates: list[Candidate] = []
        for r in rows:
            matched = [p for i, p in enumerate(phrase_list) if r.get(f"ph{i}")]
            hit = self._hit(r, 0.0)
            hit.matched_phrases = matched
            candidates.append(
                Candidate(
                    key=hit.doc_id,
                    tf=parse_tsvector_text(r.get("tsv_text") or ""),
                    doc_len=int(r["doc_len"]),
                    payload=hit,
                )
            )

        prefixes = [t[:-2] for t in query_terms]
        expanded = sorted({lex for cand in candidates for lex in cand.tf if any(lex.startswith(p) for p in prefixes)})
        stats_rows = c.execute(
            "SELECT 'corpus' AS kind, doc_count AS n, avg_doc_len AS v, CAST(NULL AS text) AS term "
            "FROM search_corpus_stats WHERE namespace = :ns "
            "UNION ALL "
            "SELECT 'term' AS kind, doc_freq AS n, CAST(NULL AS double precision) AS v, term "
            "FROM search_term_stats WHERE namespace = :ns "
            "AND term IN (SELECT jsonb_array_elements_text(CAST(:terms AS jsonb)))",
            {"ns": scope, "terms": expanded},
        )
        n_docs = 0
        avg_len = 0.0
        doc_freqs: dict[str, int] = {}
        for r in stats_rows:
            if r["kind"] == "corpus":
                n_docs = int(r["n"])
                avg_len = float(r["v"] or 0.0)
            else:
                doc_freqs[str(r["term"])] = int(r["n"])

        ranked = rank_bm25(candidates, CorpusStats(n_docs, avg_len, doc_freqs), query_terms)
        hits: list[SearchHit] = []
        for cand, score in ranked[: int(top_k)]:
            ranked_hit = cand.payload
            assert isinstance(ranked_hit, SearchHit)
            ranked_hit.score = score
            hits.append(ranked_hit)
        return hits

    def fuzzy_entities(
        self,
        scope: str,
        text: str,
        limit: int = 50,
        threshold: float = 0.3,
        recheck: bool = False,
    ) -> list[FuzzyHit]:
        """Typo-tolerant label match using pg_trgm; ``recheck`` also requires Levenshtein distance <= 2."""
        if not tokenize(text):
            return []
        recheck_sql = " AND levenshtein(lower(label), lower(:t)) <= 2" if recheck else ""
        with self._client.transaction():
            self._client.execute(
                "SELECT set_config('pg_trgm.similarity_threshold', :th, true)", {"th": str(float(threshold))}
            )
            rows = self._client.execute(
                "SELECT entity_id, label, similarity(label, :t) AS s FROM entity_label "
                f"WHERE namespace = :ns AND label % :t{recheck_sql} ORDER BY s DESC LIMIT :n",
                {"t": text, "ns": scope, "n": int(limit)},
            )
        return [FuzzyHit(str(r["entity_id"]), str(r["label"]), float(r["s"])) for r in rows]

    def _docs_for_entities(
        self, scope: str, entity_ids: Iterable[str], filters: dict[str, Any] | None
    ) -> list[SearchHit]:
        ids = sorted(set(entity_ids))
        if not ids:
            return []
        filter_sql, params = _build_filters(filters)
        params.update({"ns": scope, "ids": ids})
        rows = self._client.execute(
            "SELECT doc_id, doc_kind, body, metadata FROM search_doc WHERE namespace = :ns AND ("
            "metadata @> ANY (ARRAY(SELECT jsonb_build_object('entity_uri', e) "
            "FROM jsonb_array_elements_text(CAST(:ids AS jsonb)) AS t(e))) "
            f"OR doc_id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))){filter_sql} "
            "ORDER BY doc_id LIMIT 500",
            params,
        )
        return [self._hit(r, 0.0) for r in rows]

    def search(
        self,
        scope: str,
        query: str,
        vector: Sequence[float] | None = None,
        mode: SearchMode = SearchMode.HYBRID,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
        fuzzy_names: bool = True,
    ) -> list[SearchHit]:
        """Search a scope. Missing inputs shrink the result instead of raising."""
        if mode == SearchMode.SEMANTIC:
            return self.vector_search(scope, vector, top_k, filters) if vector is not None else []
        depth = max(top_k * 3, 30)
        if mode == SearchMode.LEXICAL or vector is None:
            return self.keyword_search(scope, query, top_k, filters=filters)

        vec_hits = self.vector_search(scope, vector, depth, filters)
        kw_hits = self.keyword_search(scope, query, depth, filters=filters)
        fuzzy = self.fuzzy_entities(scope, query, limit=depth) if fuzzy_names else []

        best: dict[str, SearchHit] = {}
        rankings: dict[str, list[str]] = {"vector": [], "bm25": [], "trigram": []}
        for name, hits in (("vector", vec_hits), ("bm25", kw_hits)):
            for hit in hits:
                if hit.key not in rankings[name]:
                    rankings[name].append(hit.key)
                best.setdefault(hit.key, hit)
        for fz in fuzzy:
            if fz.entity_id not in rankings["trigram"]:
                rankings["trigram"].append(fz.entity_id)
        missing = [k for k in rankings["trigram"] if k not in best]
        for hit in self._docs_for_entities(scope, missing, filters):
            best.setdefault(hit.key, hit)

        fused = reciprocal_rank_fusion(rankings)
        results: list[SearchHit] = []
        for f in fused:
            fused_hit = best.get(f.key)
            if fused_hit is None:
                continue
            fused_hit.score = f.score
            fused_hit.ranks = f.ranks
            results.append(fused_hit)
            if len(results) >= top_k:
                break
        return results
