# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aurora PostgreSQL implementation of :class:`GraphStore` over one RDF quad table.

Every ontology is a named graph, exactly as in the Neptune DB store: the graph URI
is ``{base}/{namespace}/{quote(ontology_id)}`` (see ``_ontology_graph_uri``) and
lives in the ``graph_uri`` column of ``coa_triple``. The ``namespace`` column plays
the role of Neptune's ``STRSTARTS(STR(?g), "{base}/{ns}/")`` scope.

Each typed method is a small number of parameterised SQL statements. Label and
kind choices reuse the Neptune store's helpers (``LabelPreference``,
``_kind_from_type``, ``_accumulate_class_row``) so both backends name things the
same way; the parity tests compare them.

Multi-hop questions use :meth:`AuroraGraphStore.traverse`: one ``WITH RECURSIVE``
statement with a mandatory depth bound and cycle handling. pgRouting is not used.

Data API constraints (1 MiB responses, 4 MiB requests, 45 s statements, no array
parameters) are handled as described in :mod:`coa_ontology.stores.aurora.vector`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import rdflib
from coa_common import validate_namespace_name
from coa_common.aurora_data_api import (
    AuroraConfigError,
    DataApiClient,
    ResponseTooLargeError,
    bulk_json_chunks,
    json_text,
)
from coa_common.constants import StructuredBackend
from coa_common.label_language import LabelPreference

from coa_ontology.stores.aurora.schema import SchemaInfo, configured_dimensions, default_client, ensure_schema
from coa_ontology.stores.base import GraphStore
from coa_ontology.stores.neptune_db_graph import (
    _KIND_TO_SPARQL_TYPES,
    _MAX_SEARCH_PAGE_SIZE,
    _SAMPLE_MAX_DESCENDANTS,
    _SAMPLE_ROOTS,
    _SCL,
    _SKOS,
    _SUPERSEDE_CHUNK,
    _XSD,
    DCT,
    DEFAULT_NAMESPACE,
    OWL,
    RDF,
    RDFS,
    WB,
    NeptuneDBGraphStore,
    _iri,
    _is_storable_iri,
    _kind_from_type,
    _kind_specificity,
    _local_name,
    _ontology_graph_uri,
    _preference,
    _storable_iris,
)

log = logging.getLogger(__name__)

MAX_TRAVERSAL_DEPTH = 32
MAX_TRAVERSAL_LIMIT = 5000
_DELETE_BATCH = 20_000
_URI_CHUNK = 1000
_VALUES_BATCH = 500

_RDF_TYPE = RDF + "type"
_RDFS_LABEL = RDFS + "label"
_RDFS_COMMENT = RDFS + "comment"
_SUBCLASS_OF = RDFS + "subClassOf"
_CLASS_TYPES = [OWL + "Class", RDFS + "Class"]
_PROPERTY_TYPES = [OWL + "ObjectProperty", OWL + "DatatypeProperty", RDF + "Property", OWL + "AnnotationProperty"]
_HISTORY_PREDS = {
    _SCL + "supersededComment": "comments",
    _SCL + "supersededDefinition": "definitions",
    _SCL + "supersededAltLabel": "altLabels",
    _SCL + "supersededGlossaryTerm": "glossaryTerms",
    _SCL + "supersededTag": "tags",
}

# The class/property triple builders of the Neptune store emit N-Triples lines. Parsing those lines
# (instead of re-implementing the triple sets) keeps both backends storing identical triples.
_NT_LINE = re.compile(r'^<([^>]*)> <([^>]*)> (?:<([^>]*)>|"((?:[^"\\]|\\.)*)"(?:\^\^<([^>]*)>)?) \.$')
_NT_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}

_IN_LIST = "(SELECT jsonb_array_elements_text(CAST(:{name} AS jsonb)))"


def _keyset(
    sql: str, params: Mapping[str, Any]
) -> Callable[[Mapping[str, Any] | None, int], tuple[str, dict[str, Any]]]:
    """Page builder for ``DataApiClient.query_paged``: fills ``:after`` (last row id) and ``:limit``."""

    def build(last: Mapping[str, Any] | None, limit: int) -> tuple[str, dict[str, Any]]:
        return sql, {**params, "after": last["id"] if last else 0, "limit": limit}

    return build


def _in_list(name: str) -> str:
    return _IN_LIST.format(name=name)


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: _NT_ESCAPES.get(m.group(1), m.group(0)), s)


def _row(s: str, p: str, o: str, o_kind: str, lang: str = "", datatype: str = "", s_kind: str = "iri") -> dict:
    if datatype == _XSD + "string":
        datatype = ""
    return {"s": s, "s_kind": s_kind, "p": p, "o": o, "o_kind": o_kind, "o_lang": lang, "o_datatype": datatype}


def _rows_from_nt(lines: Iterable[str]) -> list[dict]:
    rows = []
    for line in lines:
        m = _NT_LINE.match(line)
        if not m:
            raise ValueError(f"cannot read generated triple: {line[:120]!r}")
        s, p, iri_o, lit, dt = m.groups()
        if iri_o is not None:
            rows.append(_row(s, p, iri_o, "iri"))
        else:
            rows.append(_row(s, p, _unescape(lit or ""), "literal", datatype=dt or ""))
    return rows


def _term_kind(node: rdflib.term.Node) -> str:
    if isinstance(node, rdflib.BNode):
        return "bnode"
    return "literal" if isinstance(node, rdflib.Literal) else "iri"


def _rows_from_graph(graph: rdflib.Graph) -> list[dict]:
    rows = []
    for s, p, o in graph:
        if isinstance(o, rdflib.Literal):
            rows.append(
                _row(
                    str(s),
                    str(p),
                    str(o),
                    "literal",
                    lang=o.language or "",
                    datatype=str(o.datatype) if o.datatype else "",
                    s_kind=_term_kind(s),
                )
            )
        else:
            rows.append(_row(str(s), str(p), str(o), _term_kind(o), s_kind=_term_kind(s)))
    return rows


def _node(kind: str, value: str, *, lang: str = "", datatype: str = "") -> rdflib.term.Node:
    if kind == "bnode":
        return rdflib.BNode(value)
    if kind == "literal":
        if lang:
            return rdflib.Literal(value, lang=lang)
        return rdflib.Literal(value, datatype=rdflib.URIRef(datatype) if datatype else None)
    return rdflib.URIRef(value)


def _binding(row: Mapping[str, Any], key: str = "o") -> dict:
    """A SPARQL-JSON style binding for a stored object, so the Neptune row folding helpers can be reused."""
    kind = {"iri": "uri"}.get(row.get(f"{key}_kind", "iri"), row.get(f"{key}_kind", "iri"))
    out: dict[str, Any] = {"type": kind, "value": row[key]}
    if row.get("o_lang"):
        out["xml:lang"] = row["o_lang"]
    return out


class AuroraGraphStore(GraphStore):
    """RDF graph store on Aurora PostgreSQL, one named graph per ontology."""

    def __init__(
        self,
        namespace: str | None = None,
        *,
        client: DataApiClient | None = None,
        dimensions: int | None = None,
    ):
        """Bind a namespace; the Data API client and schema check are deferred to first use."""
        self._namespace = namespace
        self._client = client
        self._dimensions = dimensions
        self._info: SchemaInfo | None = None
        # Pure helper: builds the same triples the Neptune store writes (it does no I/O).
        self._triples = NeptuneDBGraphStore(namespace=namespace)

    # ── plumbing ────────────────────────────────────────────────────

    def _db(self) -> DataApiClient:
        if self._client is None:
            self._client = default_client()
        if self._info is None:
            self._info = ensure_schema(self._client, self._dimensions or configured_dimensions())
        return self._client

    def _ns(self, namespace: str | None = None) -> str:
        return namespace or self._namespace or DEFAULT_NAMESPACE

    def _graph_for(self, ontology_uri: str, namespace: str | None = None) -> str:
        return _ontology_graph_uri(ontology_uri, namespace or self._namespace)

    def _write_rows(self, graph_uri: str, rows: list[dict], namespace: str | None = None) -> None:
        """Insert quads (set semantics). One statement per JSON chunk, one transaction when several."""
        if not rows:
            return
        db = self._db()
        sql = (
            "INSERT INTO coa_triple (namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype) "
            "SELECT :ns, :g, r.s, r.s_kind, r.p, r.o, r.o_kind, r.o_lang, r.o_datatype "
            "FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r("
            "s text, s_kind text, p text, o text, o_kind text, o_lang text, o_datatype text) "
            "ON CONFLICT (graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype) DO NOTHING"
        )
        ns = self._ns(namespace)
        chunks = list(bulk_json_chunks(rows))
        if len(chunks) == 1:
            db.execute(sql, {"ns": ns, "g": graph_uri, "rows": chunks[0]})
            return
        with db.transaction() as tx:
            for chunk in chunks:
                tx.execute(sql, {"ns": ns, "g": graph_uri, "rows": chunk})

    def _delete_graph(self, graph_uri: str) -> int:
        db = self._db()
        total = 0
        while True:
            rows = db.execute(
                "WITH d AS (DELETE FROM coa_triple WHERE id IN "
                "(SELECT id FROM coa_triple WHERE graph_uri = :g LIMIT :batch) RETURNING 1) "
                "SELECT count(*) AS n FROM d",
                {"g": graph_uri, "batch": _DELETE_BATCH},
            )
            n = int(rows[0]["n"]) if rows else 0
            total += n
            if n < _DELETE_BATCH:
                return total

    # ── ontology CRUD ───────────────────────────────────────────────

    def create_ontology(self, data: dict) -> dict:
        """Create an ontology's metadata node in its named graph."""
        uri = data["uri"]
        _iri(uri)
        now = datetime.now(UTC).isoformat()
        graph_uri = self._graph_for(uri)
        rows = [
            _row(uri, _RDF_TYPE, OWL + "Ontology", "iri"),
            _row(uri, DCT + "created", now, "literal"),
            _row(uri, DCT + "modified", now, "literal"),
        ]
        for k in ("title", "description", "ontology_type", "format", "license", "license_tier", "source_registry"):
            v = data.get(k)
            if v:
                pred = DCT + k if k in {"title", "description"} else WB + k
                _iri(pred)
                rows.append(_row(uri, pred, str(v), "literal"))
        rows.extend(_row(uri, WB + "domainTag", str(tag), "literal") for tag in data.get("domain_tags") or [])
        for imp in data.get("imports") or []:
            _iri(imp)
            rows.append(_row(uri, OWL + "imports", imp, "iri"))
        self._write_rows(graph_uri, rows)
        return self.get_ontology_by_uri(uri) or {"uri": uri, "id": uri}

    def get_ontology(self, oid: str) -> dict | None:
        """Fetch an ontology by id (aliases the ontology URI)."""
        return self.get_ontology_by_uri(oid)

    def get_ontology_by_uri(self, uri: str) -> dict | None:
        """Fetch an ontology's metadata from its named graph."""
        try:
            _iri(uri)
        except ValueError:
            return None
        rows = self._db().query_paged(
            lambda last, limit: (
                "SELECT id, p, o FROM coa_triple WHERE graph_uri = :g AND s = :s AND id > :after "
                "ORDER BY id LIMIT :limit",
                {"g": self._graph_for(uri), "s": uri, "after": last["id"] if last else 0, "limit": limit},
            ),
            page_size=500,
        )
        if not rows:
            return None
        out: dict[str, Any] = {"uri": uri, "id": uri, "domain_tags": [], "imports": []}
        for r in rows:
            p, o = r["p"], r["o"]
            if p == WB + "domainTag":
                out["domain_tags"].append(o)
            elif p == OWL + "imports":
                out["imports"].append(o)
            elif p == _RDF_TYPE:
                continue
            elif p.startswith(WB):
                out[p[len(WB) :]] = o
            elif p.startswith(DCT):
                out[p[len(DCT) :]] = o
        return out

    def update_ontology(self, uri: str, updates: dict) -> dict | None:
        """Replace workbench-namespaced attributes on an ontology (``None`` clears one)."""
        if not self.get_ontology_by_uri(uri):
            return None
        graph_uri = self._graph_for(uri)
        preds: list[str] = []
        rows: list[dict] = []
        for k, v in updates.items():
            if k in {"id", "uri"}:
                continue
            pred = WB + k
            _iri(pred)
            preds.append(pred)
            if v is not None:
                rows.append(_row(uri, pred, str(v), "literal"))
        if not preds:
            return self.get_ontology_by_uri(uri)
        db = self._db()
        with db.transaction() as tx:
            tx.execute(
                f"DELETE FROM coa_triple WHERE graph_uri = :g AND s = :s AND p IN {_in_list('preds')}",
                {"g": graph_uri, "s": uri, "preds": json_text(preds)},
            )
            if rows:
                tx.execute(
                    "INSERT INTO coa_triple (namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype) "
                    "SELECT :ns, :g, r.s, r.s_kind, r.p, r.o, r.o_kind, r.o_lang, r.o_datatype "
                    "FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r("
                    "s text, s_kind text, p text, o text, o_kind text, o_lang text, o_datatype text) "
                    "ON CONFLICT (graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype) DO NOTHING",
                    {"ns": self._ns(), "g": graph_uri, "rows": json_text(rows)},
                )
        return self.get_ontology_by_uri(uri)

    def delete_ontology(self, uri: str) -> bool:
        """Drop the ontology's named graph. Returns whether its metadata node existed."""
        existing = self.get_ontology_by_uri(uri)
        self._delete_graph(self._graph_for(uri))
        return existing is not None

    # ── schema elements ─────────────────────────────────────────────

    def store_class(
        self,
        ontology_uri,
        class_uri,
        labels=None,
        comments=None,
        super_classes=None,
        is_mapped=False,
        query_backend=None,
        data_source_id=None,
    ):
        """Project a class (labels, comments, superclasses, markers) into the ontology graph."""
        lines = self._triples._class_triples(
            ontology_uri, class_uri, labels, comments, super_classes, is_mapped, query_backend, data_source_id
        )
        self._write_rows(self._graph_for(ontology_uri), _rows_from_nt(lines))
        return {"uri": class_uri, "ontology_id": ontology_uri}

    def store_classes_batch(self, ontology_uri, classes: list[dict]) -> int:
        """Project many classes into the ontology graph; returns the number of classes."""
        rows: list[dict] = []
        for c in classes:
            lines = self._triples._class_triples(
                ontology_uri,
                c["class_uri"],
                c.get("labels"),
                c.get("comments"),
                c.get("super_classes"),
                c.get("is_mapped", False),
                c.get("query_backend"),
                c.get("data_source_id"),
            )
            rows.extend(_rows_from_nt(lines))
        self._write_rows(self._graph_for(ontology_uri), rows)
        return len(classes)

    def store_property(self, ontology_uri, prop_uri, label="", comment="", domains=None, ranges=None):
        """Project a property (label, comment, domains, ranges) into the ontology graph."""
        lines = self._triples._property_triples(ontology_uri, prop_uri, label, comment, domains, ranges)
        self._write_rows(self._graph_for(ontology_uri), _rows_from_nt(lines))
        return {"uri": prop_uri, "ontology_id": ontology_uri}

    def store_properties_batch(self, ontology_uri, properties: list[dict]) -> int:
        """Project many properties into the ontology graph; returns the number of properties."""
        rows: list[dict] = []
        for p in properties:
            lines = self._triples._property_triples(
                ontology_uri,
                p["prop_uri"],
                p.get("label", ""),
                p.get("comment", ""),
                p.get("domains"),
                p.get("ranges"),
            )
            rows.extend(_rows_from_nt(lines))
        self._write_rows(self._graph_for(ontology_uri), rows)
        return len(properties)

    def supersede_annotations(
        self,
        ontology_uri: str,
        subject_uris: Iterable[str],
        predicates: Mapping[str, str | None],
    ) -> int:
        """Retire the live ``predicates`` values of ``subject_uris``; displaced values move to the history predicate.

        One statement per chunk of subjects, scoped to the (subject, predicate) pairs given, so
        relationship axioms and other subjects in the graph are untouched. Returns the number of
        live triples retired.
        """
        subjects = [s for s in dict.fromkeys(subject_uris) if _is_storable_iri(s)]
        if not subjects or not predicates:
            return 0
        pairs = json_text([{"live": live, "hist": hist or None} for live, hist in predicates.items()])
        graph_uri = self._graph_for(ontology_uri)
        db = self._db()
        removed = 0
        for i in range(0, len(subjects), _SUPERSEDE_CHUNK):
            rows = db.execute(
                "WITH subj AS (SELECT jsonb_array_elements_text(CAST(:subjects AS jsonb)) AS s), "
                "pm AS (SELECT x.live, x.hist "
                "FROM jsonb_to_recordset(CAST(:pairs AS jsonb)) AS x(live text, hist text)), "
                "moved AS (INSERT INTO coa_triple (namespace, graph_uri, s, s_kind, p, o, o_kind, o_lang, o_datatype) "
                "SELECT t.namespace, t.graph_uri, t.s, t.s_kind, pm.hist, t.o, t.o_kind, t.o_lang, t.o_datatype "
                "FROM coa_triple t JOIN pm ON t.p = pm.live "
                "WHERE t.graph_uri = :g AND t.s IN (SELECT s FROM subj) AND pm.hist IS NOT NULL "
                "ON CONFLICT (graph_uri, s, p, o_kind, md5(o), o_lang, o_datatype) DO NOTHING), "
                "gone AS (DELETE FROM coa_triple t USING pm "
                "WHERE t.graph_uri = :g AND t.s IN (SELECT s FROM subj) AND t.p = pm.live RETURNING 1) "
                "SELECT count(*) AS n FROM gone",
                {"g": graph_uri, "subjects": json_text(subjects[i : i + _SUPERSEDE_CHUNK]), "pairs": pairs},
            )
            removed += int(rows[0]["n"]) if rows else 0
        log.info("supersede_annotations: retired %d triples (subjects=%d)", removed, len(subjects))
        return removed

    # ── Turtle in and out ───────────────────────────────────────────

    def load_turtle(self, ontology_uri: str, turtle: str, graph_uri: str | None = None) -> dict:
        """Parse Turtle with rdflib and insert its triples into the ontology's named graph."""
        if not turtle or not turtle.strip():
            return {"status": "skipped", "reason": "empty turtle"}
        target = graph_uri or self._graph_for(ontology_uri)
        parsed = rdflib.Graph()
        parsed.parse(data=turtle, format="turtle")
        self._write_rows(target, _rows_from_graph(parsed))
        return {"graph_uri": target, "bytes_loaded": len(turtle.encode("utf-8")), "status": "ok"}

    def get_graph_turtle(self, ontology_id: str) -> str | None:
        """Serialize the ontology's named graph as Turtle; ``None`` when it holds no triples."""
        graph_uri = self._graph_for(ontology_id)
        db = self._db()
        out = rdflib.Graph()
        count = 0
        for page in db.iter_pages(
            lambda last, limit: (
                "SELECT id, s, s_kind, p, o, o_kind, o_lang, o_datatype FROM coa_triple "
                "WHERE graph_uri = :g AND id > :after ORDER BY id LIMIT :limit",
                {"g": graph_uri, "after": last["id"] if last else 0, "limit": limit},
            ),
            page_size=500,
        ):
            for r in page:
                out.add(
                    (
                        _node(r["s_kind"], r["s"]),
                        rdflib.URIRef(r["p"]),
                        _node(r["o_kind"], r["o"], lang=r["o_lang"], datatype=r["o_datatype"]),
                    )
                )
                count += 1
        if count == 0:
            return None
        return out.serialize(format="turtle")

    # ── counts ──────────────────────────────────────────────────────

    def count_classes_and_properties(self, ontology_id: str) -> dict | None:
        """Count distinct classes and (object + datatype) properties in the ontology's graph."""
        try:
            rows = self._db().execute(
                "SELECT count(DISTINCT s) FILTER (WHERE o = :cls) AS classes, "
                "count(DISTINCT s) FILTER (WHERE o IN (:op, :dp)) AS props "
                "FROM coa_triple WHERE graph_uri = :g AND p = :type AND o_kind = 'iri'",
                {
                    "g": self._graph_for(ontology_id),
                    "type": _RDF_TYPE,
                    "cls": OWL + "Class",
                    "op": OWL + "ObjectProperty",
                    "dp": OWL + "DatatypeProperty",
                },
            )
            if rows:
                return {"classes": int(rows[0]["classes"]), "properties": int(rows[0]["props"])}
        except Exception:
            log.warning("count_classes_and_properties failed", exc_info=True)
        return None

    # ── vertex lookup ───────────────────────────────────────────────

    def get_vertex(
        self,
        vertex_uri: str,
        namespace: str | None = None,
        *,
        label_preference: LabelPreference | None = None,
    ) -> dict | None:
        """Outgoing and incoming triples of one vertex across every graph of the namespace, plus neighbor metadata."""
        _iri(vertex_uri)
        preference = _preference(label_preference)
        ns = self._ns(namespace)
        db = self._db()

        def own(direction: str):
            sub = "s" if direction == "out" else "o"
            extra = "" if direction == "out" else " AND o_kind = 'iri'"
            sel = "p, o, o_kind, o_lang" if direction == "out" else "p, s AS o, s_kind AS o_kind, '' AS o_lang"

            def build(last, limit):
                return (
                    f"SELECT id, graph_uri, {sel} FROM coa_triple "
                    f"WHERE namespace = :ns AND {sub} = :v{extra} AND id > :after ORDER BY id LIMIT :limit",
                    {"ns": ns, "v": vertex_uri, "after": last["id"] if last else 0, "limit": limit},
                )

            return db.query_paged(build, page_size=300)

        outgoing = own("out")
        incoming = own("in")
        if not outgoing and not incoming:
            log.info("get_vertex: no triples for %s in namespace %s", vertex_uri, namespace)
            return None

        types: list[str] = []
        labels: list[tuple[str, str]] = []
        comments: list[tuple[str, str]] = []
        alt_labels: list[tuple[str, str]] = []
        superseded: dict[str, list[str]] = {}
        edges: list[dict] = []
        graph_uris: set[str] = set()
        neighbor_uris: set[str] = set()
        is_mapped = False
        skos_alt_label = _SKOS + "altLabel"

        for r in outgoing:
            graph_uris.add(r["graph_uri"])
            p, o_value, o_type = r["p"], r["o"], {"iri": "uri"}.get(r["o_kind"], r["o_kind"])
            cand = (o_value, r.get("o_lang") or "")
            if p == _RDF_TYPE and o_type == "uri":
                if o_value not in types:
                    types.append(o_value)
            elif p == _RDFS_LABEL and o_type == "literal":
                labels.append(cand)
            elif p == _RDFS_COMMENT and o_type == "literal":
                comments.append(cand)
            elif p == skos_alt_label and o_type == "literal":
                alt_labels.append(cand)
            elif p in _HISTORY_PREDS and o_type == "literal":
                bucket = superseded.setdefault(_HISTORY_PREDS[p], [])
                if o_value not in bucket:
                    bucket.append(o_value)
            elif p == _SCL + "isMapped":
                is_mapped = str(o_value).lower() == "true"
            else:
                edges.append({"p": p, "o": o_value, "o_type": o_type, "direction": "outgoing"})
                if o_type == "uri":
                    neighbor_uris.add(o_value)

        for r in incoming:
            graph_uris.add(r["graph_uri"])
            if r["o_kind"] != "iri":  # blank-node subjects (RDF list cells, restrictions) are not neighbors
                continue
            if r["p"] == _RDF_TYPE:
                continue
            edges.append({"p": r["p"], "o": r["o"], "o_type": "uri", "direction": "incoming"})
            neighbor_uris.add(r["o"])

        label_rows: list[tuple[str, str, str]] = []
        neighbor_kinds: dict[str, str] = {}
        uri_list = sorted(neighbor_uris)
        for start in range(0, len(uri_list), _VALUES_BATCH):
            batch = uri_list[start : start + _VALUES_BATCH]
            for r in db.query_paged(
                _keyset(
                    "SELECT id, s, p, o, o_lang FROM coa_triple "
                    f"WHERE namespace = :ns AND s IN {_in_list('uris')} "
                    "AND ((p = :label AND o_kind = 'literal') OR (p = :type AND o_kind = 'iri')) "
                    "AND id > :after ORDER BY id LIMIT :limit",
                    {"ns": ns, "uris": json_text(batch), "label": _RDFS_LABEL, "type": _RDF_TYPE},
                ),
                page_size=500,
            ):
                if r["p"] == _RDFS_LABEL:
                    label_rows.append((r["s"], r["o"], r.get("o_lang") or ""))
                else:
                    kind = _kind_from_type(r["o"])
                    uri = r["s"]
                    if kind and (
                        uri not in neighbor_kinds or _kind_specificity(kind) > _kind_specificity(neighbor_kinds[uri])
                    ):
                        neighbor_kinds[uri] = kind
        neighbor_labels = {u: lbl for u, lbl in preference.pick_by_resource(label_rows).items() if lbl}

        out_edges: list[dict] = []
        for e in edges:
            o_value, o_type = e["o"], e["o_type"]
            if o_type == "uri":
                neighbor = {
                    "uri": o_value,
                    "label": neighbor_labels.get(o_value),
                    "kind": neighbor_kinds.get(o_value, "unknown"),
                }
            elif o_type == "literal":
                neighbor = {"uri": o_value, "label": o_value, "kind": "literal"}
            else:
                neighbor = {"uri": o_value, "label": None, "kind": "blank-node"}
            out_edges.append(
                {
                    "predicate": e["p"],
                    "predicate_label": _local_name(e["p"]),
                    "direction": e["direction"],
                    "neighbor": neighbor,
                }
            )
        return {
            "uri": vertex_uri,
            "graph_uris": sorted(graph_uris),
            "types": types,
            "labels": preference.order(labels),
            "comments": preference.order(comments),
            "alt_labels": preference.order(alt_labels),
            "superseded": superseded,
            "is_mapped": is_mapped,
            "edges": out_edges,
        }

    # ── entity search ───────────────────────────────────────────────

    def _match_cte(
        self,
        query: str,
        namespace: str | None,
        kind: str | None,
        ontology_id: str | None,
        exclude_ontology_id: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """CTEs ``m(s, sort_key)`` = the distinct entities matching the search. Shared by the page and the count."""
        ns = self._ns(namespace)
        params: dict[str, Any] = {"ns": ns, "label": _RDFS_LABEL}
        scope = "t.namespace = :ns"
        if ontology_id:
            scope += " AND t.graph_uri = :g"
            params["g"] = _ontology_graph_uri(ontology_id, ns)
        if exclude_ontology_id:
            scope += " AND t.graph_uri <> :xg"
            params["xg"] = _ontology_graph_uri(exclude_ontology_id, ns)
        type_uris = _KIND_TO_SPARQL_TYPES.get(kind) if kind else None
        if type_uris:
            scope += f" AND t.p = :type AND t.o_kind = 'iri' AND t.o IN {_in_list('kinds')}"
            params["type"] = _RDF_TYPE
            params["kinds"] = json_text(type_uris)
        match_all = query.strip() in ("", "*")
        where = ""
        if not match_all:
            where = (
                "WHERE strpos(lower(regexp_replace(c.s, '^.*[#/]', '')), :q) > 0 "
                "OR strpos(lower(COALESCE(l.o, '')), :q) > 0"
            )
            params["q"] = query.lower()
        cte = (
            f"WITH cand AS (SELECT DISTINCT t.s, t.graph_uri FROM coa_triple t WHERE {scope}), "
            "cl AS (SELECT c.s, c.graph_uri, l.o AS label FROM cand c "
            "LEFT JOIN coa_triple l ON l.graph_uri = c.graph_uri AND l.s = c.s AND l.p = :label "
            "AND l.o_kind = 'literal' "
            f"{where}), "
            'm AS (SELECT s, MIN(lower(COALESCE(label, s)) COLLATE "C") AS sort_key FROM cl GROUP BY s)'
        )
        return cte, params

    def count_entities(
        self,
        query: str,
        namespace: str | None = None,
        kind: str | None = None,
        ontology_id: str | None = None,
        exclude_ontology_id: str | None = None,
    ) -> int:
        """Total number of distinct entities matching the search, independent of paging."""
        cte, params = self._match_cte(query, namespace, kind, ontology_id, exclude_ontology_id)
        rows = self._db().execute(f"{cte} SELECT count(*) AS total FROM m", params)
        return int(rows[0]["total"]) if rows else 0

    def search_entities(
        self,
        query: str,
        namespace: str | None = None,
        kind: str | None = None,
        ontology_id: str | None = None,
        exclude_ontology_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        label_preference: LabelPreference | None = None,
    ) -> list[dict]:
        """Substring search over local names and labels; stable order (lower label or IRI, then IRI)."""
        preference = _preference(label_preference)
        cte, params = self._match_cte(query, namespace, kind, ontology_id, exclude_ontology_id)
        safe_limit = max(1, min(limit, _MAX_SEARCH_PAGE_SIZE))
        rows = self._search_page(cte, params, max(0, offset), safe_limit, ontology_id, exclude_ontology_id)

        hits: dict[str, dict] = {}
        graphs_for: dict[str, list[str]] = {}
        label_rows: list[tuple[str, str, str]] = []
        for r in rows:
            uri = r["s"]
            lst = graphs_for.setdefault(uri, [])
            if r["g"] not in lst:
                lst.append(r["g"])
            if r.get("label") is not None:
                label_rows.append((uri, r["label"], r.get("label_lang") or ""))
            new_kind = _kind_from_type(r["t"]) if r.get("t") else None
            if uri in hits:
                if new_kind and _kind_specificity(new_kind) > _kind_specificity(hits[uri].get("kind", "unknown")):
                    hits[uri]["kind"] = new_kind
                continue
            hits[uri] = {"uri": uri, "label": None, "kind": new_kind or "unknown"}
        label_of = preference.pick_by_resource(label_rows)
        for uri, entry in hits.items():
            entry["label"] = label_of.get(uri) or None
            entry["graph_uris"] = graphs_for.get(uri, [])
        return list(hits.values())

    def _search_page(
        self,
        cte: str,
        params: dict[str, Any],
        offset: int,
        limit: int,
        ontology_id: str | None,
        exclude_ontology_id: str | None,
    ) -> list[dict]:
        """Read one page window; if the response is over 1 MiB, read the two halves separately."""
        scope = "t.namespace = :ns"
        if ontology_id:
            scope += " AND t.graph_uri = :g"
        if exclude_ontology_id:
            scope += " AND t.graph_uri <> :xg"
        sql = (
            f'{cte}, pg AS (SELECT s, sort_key FROM m ORDER BY sort_key, s COLLATE "C" LIMIT :limit OFFSET :offset) '
            "SELECT p.s, p.sort_key, g.graph_uri AS g, lab.o AS label, lab.o_lang AS label_lang, ty.o AS t "
            "FROM pg p "
            "JOIN (SELECT DISTINCT t.s, t.graph_uri FROM coa_triple t "
            f"WHERE {scope} AND t.s IN (SELECT s FROM pg)) g ON g.s = p.s "
            "LEFT JOIN coa_triple lab ON lab.graph_uri = g.graph_uri AND lab.s = p.s AND lab.p = :label "
            "AND lab.o_kind = 'literal' "
            "LEFT JOIN coa_triple ty ON ty.graph_uri = g.graph_uri AND ty.s = p.s AND ty.p = :rdftype "
            "AND ty.o_kind = 'iri' "
            'ORDER BY p.sort_key, p.s COLLATE "C", g.graph_uri, lab.o, ty.o'
        )
        call = {**params, "rdftype": _RDF_TYPE, "limit": limit, "offset": offset}
        try:
            rows = self._db().execute(sql, call)
        except ResponseTooLargeError:
            if limit == 1:
                raise
            first = limit // 2
            return self._search_page(cte, params, offset, first, ontology_id, exclude_ontology_id) + self._search_page(
                cte, params, offset + first, limit - first, ontology_id, exclude_ontology_id
            )
        return rows

    # ── mapped-class lookups ────────────────────────────────────────

    def mapped_class_backends(self, class_uris: Iterable[str], namespace: str | None = None) -> dict[str, str]:
        """Which backend answers each class already (``coa:isMapped``), across the namespace's graphs."""
        uris = _storable_iris(class_uris)
        if not uris:
            return {}
        ns = self._ns(namespace)
        db = self._db()
        out: dict[str, str] = {}
        for i in range(0, len(uris), _URI_CHUNK):
            rows = db.execute(
                "SELECT DISTINCT m.s AS c, b.o AS backend FROM coa_triple m "
                "LEFT JOIN coa_triple b ON b.graph_uri = m.graph_uri AND b.s = m.s AND b.p = :qb "
                f"WHERE m.namespace = :ns AND m.p = :mapped AND m.s IN {_in_list('uris')}",
                {
                    "ns": ns,
                    "qb": _SCL + "queryBackend",
                    "mapped": _SCL + "isMapped",
                    "uris": json_text(uris[i : i + _URI_CHUNK]),
                },
            )
            for r in rows:
                out[r["c"]] = r.get("backend") or StructuredBackend.SQL.value
        return out

    def mapped_class_graph_sources(
        self, class_uris: Iterable[str], namespace: str | None = None
    ) -> dict[str, set[str]]:
        """The graph sources (``coa:dataSourceId``) each class is already bound to, across the namespace."""
        uris = _storable_iris(class_uris)
        if not uris:
            return {}
        ns = self._ns(namespace)
        db = self._db()
        sources: dict[str, set[str]] = {}
        for i in range(0, len(uris), _URI_CHUNK):
            rows = db.execute(
                "SELECT DISTINCT q.s AS c, d.o AS src FROM coa_triple q "
                "JOIN coa_triple d ON d.graph_uri = q.graph_uri AND d.s = q.s AND d.p = :ds "
                "WHERE q.namespace = :ns AND q.p = :qb AND q.o = :rdf_sparql AND q.o_kind = 'literal' "
                f"AND q.s IN {_in_list('uris')}",
                {
                    "ns": ns,
                    "ds": _SCL + "dataSourceId",
                    "qb": _SCL + "queryBackend",
                    "rdf_sparql": StructuredBackend.RDF_SPARQL.value,
                    "uris": json_text(uris[i : i + _URI_CHUNK]),
                },
            )
            for r in rows:
                sources.setdefault(r["c"], set()).add(r["src"])
        return sources

    # ── ontology overview ───────────────────────────────────────────

    def _annotation_rows(self, graph_uri: str, subject_types: list[str], predicates: list[str]) -> list[dict]:
        """Type rows plus the annotation rows of typed subjects in one graph, keyset-paged by id."""
        type_scope = f"ty.p = :type AND ty.o_kind = 'iri' AND ty.o IN {_in_list('types')}"
        base = {
            "g": graph_uri,
            "type": _RDF_TYPE,
            "types": json_text(subject_types),
            "preds": json_text(predicates),
        }

        def build(last, limit):
            return (
                "SELECT t.id, t.s, t.p, t.o, t.o_kind, t.o_lang FROM coa_triple t "
                "WHERE t.graph_uri = :g AND t.id > :after AND ("
                f"(t.p = :type AND t.o_kind = 'iri' AND t.o IN {_in_list('types')}) "
                f"OR (t.p IN {_in_list('preds')} AND EXISTS ("
                f"SELECT 1 FROM coa_triple ty WHERE ty.graph_uri = t.graph_uri AND ty.s = t.s AND {type_scope}))) "
                "ORDER BY t.id LIMIT :limit",
                {**base, "after": last["id"] if last else 0, "limit": limit},
            )

        return self._db().query_paged(build, page_size=400)

    def _class_entries(self, graph_uri: str, preference: LabelPreference) -> dict[str, dict]:
        classes: dict[str, dict] = {}
        slots = {
            _RDFS_LABEL: "label",
            _RDFS_COMMENT: "comment",
            _SCL + "groundedTo": "groundedTo",
            _SKOS + "exactMatch": "exact",
            _SKOS + "closeMatch": "close",
            _SUBCLASS_OF: "parent",
        }
        # Neptune receives one row per label x comment x grounding combination, so a class that has both
        # coa:groundedTo and skos:exactMatch is grounded to groundedTo. Here every predicate is its own row, and
        # the first grounding row wins in _accumulate_class_row, so feed them in that priority order.
        priority = {_SCL + "groundedTo": 1, _SKOS + "exactMatch": 2, _SKOS + "closeMatch": 3}
        rows = sorted(
            self._annotation_rows(graph_uri, _CLASS_TYPES, list(slots)), key=lambda r: priority.get(r["p"], 0)
        )
        for r in rows:
            b: dict[str, Any] = {"c": {"type": "uri", "value": r["s"]}}
            if r["p"] != _RDF_TYPE:
                b[slots[r["p"]]] = _binding(r)
            NeptuneDBGraphStore._accumulate_class_row(classes, b)
        NeptuneDBGraphStore._finalize_class_rows(classes, preference)
        return classes

    def get_ontology_overview(
        self,
        ontology_id: str,
        namespace: str | None = None,
        *,
        sample: bool = False,
        label_preference: LabelPreference | None = None,
    ) -> dict | None:
        """Every class and property of one ontology graph, or a bounded taxonomy seed when ``sample``."""
        ns = namespace or self._namespace
        graph_uri = _ontology_graph_uri(ontology_id, ns)
        preference = _preference(label_preference)
        classes = self._class_entries(graph_uri, preference)
        if sample:
            return self._sample_overview(ontology_id, graph_uri, ns, classes)

        props: dict[str, dict] = {}
        slots = {_RDFS_LABEL: "label", RDFS + "domain": "domain", RDFS + "range": "range"}
        for r in self._annotation_rows(graph_uri, _PROPERTY_TYPES, list(slots)):
            entry = props.setdefault(
                r["s"], {"uri": r["s"], "label": None, "domain": None, "range": None, "types": set(), "_labels": []}
            )
            if r["p"] == _RDF_TYPE:
                entry["types"].add(r["o"])
            elif r["p"] == _RDFS_LABEL:
                entry["_labels"].append((r["o"], r.get("o_lang") or ""))
            elif r["o_kind"] == "iri":
                slot = slots[r["p"]]
                if entry[slot] is None:
                    entry[slot] = r["o"]

        object_properties: list[dict] = []
        datatype_properties: list[dict] = []
        for entry in props.values():
            entry["label"] = preference.pick(entry.pop("_labels")) or None
            types = entry.pop("types")
            is_object = OWL + "ObjectProperty" in types
            is_datatype = OWL + "DatatypeProperty" in types
            if OWL + "AnnotationProperty" in types and not is_object and not is_datatype:
                continue
            rng = entry.get("range") or ""
            is_literal_range = rng.startswith(_XSD) or rng == RDFS + "Literal"
            if is_datatype or (not is_object and is_literal_range):
                datatype_properties.append(entry)
            else:
                object_properties.append(entry)
        return {
            "ontology_id": ontology_id,
            "graph_uri": graph_uri,
            "classes": sorted(classes.values(), key=lambda c: (c["label"] or c["uri"]).lower()),
            "object_properties": sorted(object_properties, key=lambda p: (p["label"] or p["uri"]).lower()),
            "datatype_properties": sorted(datatype_properties, key=lambda p: (p["label"] or p["uri"]).lower()),
        }

    def _sample_overview(
        self, ontology_id: str, graph_uri: str, namespace: str | None, classes: dict[str, dict]
    ) -> dict:
        """Bounded taxonomy seed: the largest-subtree roots and a capped set of their descendants.

        Subtree sizes come from one recursive-CTE statement (:meth:`_descendant_counts`) instead of a Python
        walk per root. The breadth-first seed is the Neptune store's algorithm, ported unchanged.
        """
        children_of: dict[str, list[str]] = {uri: [] for uri in classes}
        for uri, entry in classes.items():
            for parent in entry["sub_class_of"]:
                if parent in classes:
                    children_of[parent].append(uri)

        roots = [uri for uri, entry in classes.items() if not any(p in classes for p in entry["sub_class_of"])]
        sizes = self._descendant_counts(roots, graph_uri, namespace)
        roots.sort(key=lambda r: (-sizes.get(r, 0), (classes[r]["label"] or r).lower()))
        seed_roots = roots[:_SAMPLE_ROOTS]

        included: dict[str, None] = {r: None for r in seed_roots}
        frontier = list(seed_roots)
        while frontier and len(included) - len(seed_roots) < _SAMPLE_MAX_DESCENDANTS:
            nxt: list[str] = []
            for uri in frontier:
                for child in children_of.get(uri, ()):
                    if child in included:
                        continue
                    included[child] = None
                    nxt.append(child)
                    if len(included) - len(seed_roots) >= _SAMPLE_MAX_DESCENDANTS:
                        break
                if len(included) - len(seed_roots) >= _SAMPLE_MAX_DESCENDANTS:
                    break
            frontier = nxt

        sampled: list[dict] = []
        for uri in included:
            entry = dict(classes[uri])
            entry["sub_class_of"] = [p for p in entry["sub_class_of"] if p in included]
            sampled.append(entry)
        sampled.sort(key=lambda c: (c["label"] or c["uri"]).lower())
        return {
            "ontology_id": ontology_id,
            "graph_uri": graph_uri,
            "classes": sampled,
            "object_properties": [],
            "datatype_properties": [],
        }

    # ── namespace schema ────────────────────────────────────────────

    def get_namespace_schema(
        self,
        namespace: str,
        *,
        max_results: int = 100,
        include_properties: bool = True,
        label_preference: LabelPreference | None = None,
        class_prefix: str | None = None,
    ) -> dict:
        """Classes (mapped first, then by label) and their datatype properties across the namespace's graphs."""
        preference = _preference(label_preference)
        validate_namespace_name(namespace)
        db = self._db()
        params: dict[str, Any] = {
            "ns": namespace,
            "type": _RDF_TYPE,
            "ctypes": json_text(_CLASS_TYPES),
            "label": _RDFS_LABEL,
            "mapped": _SCL + "isMapped",
            "n": max_results,
        }
        prefix_sql = ""
        if class_prefix:
            if class_prefix.endswith(("#", "/")):
                prefix_sql = " AND starts_with(t.s, :pfx)"
                params["pfx"] = class_prefix
            else:
                prefix_sql = " AND (t.s = :pfx OR starts_with(t.s, :pfx_hash) OR starts_with(t.s, :pfx_slash))"
                params.update({"pfx": class_prefix, "pfx_hash": class_prefix + "#", "pfx_slash": class_prefix + "/"})
        page = db.execute(
            "SELECT t.s AS class_uri, "
            "MIN(CASE WHEN m.o = 'true' THEN 0 ELSE 1 END) AS mapped_rank, "
            'MIN(lower(COALESCE(l.o, t.s)) COLLATE "C") AS sort_key '
            "FROM coa_triple t "
            "LEFT JOIN coa_triple l ON l.graph_uri = t.graph_uri AND l.s = t.s AND l.p = :label "
            "LEFT JOIN coa_triple m ON m.graph_uri = t.graph_uri AND m.s = t.s AND m.p = :mapped "
            f"WHERE t.namespace = :ns AND t.p = :type AND t.o_kind = 'iri' AND t.o IN {_in_list('ctypes')}{prefix_sql} "
            'GROUP BY t.s ORDER BY mapped_rank, sort_key, t.s COLLATE "C" LIMIT :n',
            params,
        )
        grouped: dict[str, dict] = {r["class_uri"]: {"labels": [], "descriptions": [], "parent": ""} for r in page}
        uris = list(grouped)
        for i in range(0, len(uris), _URI_CHUNK):
            part = uris[i : i + _URI_CHUNK]
            for r in db.query_paged(
                _keyset(
                    "SELECT t.id, t.s, t.p, t.o, t.o_lang FROM coa_triple t "
                    f"WHERE t.namespace = :ns AND t.s IN {_in_list('uris')} "
                    "AND t.p IN (:label, :comment, :sub) AND t.id > :after "
                    "AND EXISTS (SELECT 1 FROM coa_triple ty WHERE ty.graph_uri = t.graph_uri AND ty.s = t.s "
                    f"AND ty.p = :type AND ty.o_kind = 'iri' AND ty.o IN {_in_list('ctypes')}) "
                    "ORDER BY t.id LIMIT :limit",
                    {
                        "ns": namespace,
                        "uris": json_text(part),
                        "label": _RDFS_LABEL,
                        "comment": _RDFS_COMMENT,
                        "sub": _SUBCLASS_OF,
                        "type": _RDF_TYPE,
                        "ctypes": json_text(_CLASS_TYPES),
                    },
                ),
                page_size=300,
            ):
                entry = grouped[r["s"]]
                if r["p"] == _RDFS_LABEL:
                    entry["labels"].append((r["o"], r.get("o_lang") or ""))
                elif r["p"] == _RDFS_COMMENT:
                    entry["descriptions"].append((r["o"], r.get("o_lang") or ""))
                elif not entry["parent"]:
                    entry["parent"] = r["o"]
        classes = [
            {
                "uri": uri,
                "label": preference.pick(entry["labels"]),
                "description": preference.pick(entry["descriptions"]),
                "parentClass": entry["parent"],
                "properties": [],
            }
            for uri, entry in grouped.items()
        ]

        domains = sorted({c["uri"] for c in classes if _is_storable_iri(c["uri"])})
        if include_properties and domains:
            prows = db.execute(
                "SELECT pr.s AS prop_uri, d.o AS domain, l.o AS label, l.o_lang AS label_lang, "
                "r.o AS range, c.o AS descr, c.o_lang AS descr_lang "
                "FROM coa_triple pr "
                "JOIN coa_triple d ON d.graph_uri = pr.graph_uri AND d.s = pr.s AND d.p = :domain "
                f"AND d.o IN {_in_list('domains')} "
                "LEFT JOIN coa_triple l ON l.graph_uri = pr.graph_uri AND l.s = pr.s AND l.p = :label "
                "LEFT JOIN coa_triple r ON r.graph_uri = pr.graph_uri AND r.s = pr.s AND r.p = :range "
                "LEFT JOIN coa_triple c ON c.graph_uri = pr.graph_uri AND c.s = pr.s AND c.p = :comment "
                "WHERE pr.namespace = :ns AND pr.p = :type AND pr.o = :dtp AND pr.o_kind = 'iri' "
                'ORDER BY l.o COLLATE "C" ASC NULLS FIRST, pr.s COLLATE "C", d.o COLLATE "C", r.o, c.o LIMIT :n',
                {
                    "ns": namespace,
                    "type": _RDF_TYPE,
                    "dtp": OWL + "DatatypeProperty",
                    "domain": RDFS + "domain",
                    "range": RDFS + "range",
                    "label": _RDFS_LABEL,
                    "comment": _RDFS_COMMENT,
                    "domains": json_text(domains),
                    "n": max_results * 5,
                },
            )
            grouped_props: dict[tuple[str, str], dict] = {}
            for r in prows:
                entry = grouped_props.setdefault(
                    (r["domain"], r["prop_uri"]), {"labels": [], "descriptions": [], "range": ""}
                )
                if r.get("label") is not None:
                    entry["labels"].append((r["label"], r.get("label_lang") or ""))
                if r.get("descr") is not None:
                    entry["descriptions"].append((r["descr"], r.get("descr_lang") or ""))
                if not entry["range"] and r.get("range"):
                    entry["range"] = r["range"]
            props_by_domain: dict[str, list[dict]] = {}
            for (domain, prop_uri), entry in grouped_props.items():
                props_by_domain.setdefault(domain, []).append(
                    {
                        "uri": prop_uri,
                        "label": preference.pick(entry["labels"]),
                        "range": entry["range"],
                        "description": preference.pick(entry["descriptions"]),
                    }
                )
            for cls in classes:
                cls["properties"] = props_by_domain.get(cls["uri"], [])
        return {"classes": classes, "namespace": namespace, "ontologyVersion": ""}

    # ── traversal ───────────────────────────────────────────────────

    def _walk_cte(
        self,
        start_uris: Sequence[str],
        *,
        predicates: Sequence[str] | None,
        direction: str,
        max_depth: int,
        namespace: str | None,
        ontology_id: str | None,
        return_paths: bool,
        node_types: Sequence[str] | None,
        graph_uri: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Build the ``WITH RECURSIVE walk`` clause and its parameters."""
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be 'out', 'in' or 'both', got {direction!r}")
        if not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH:
            raise ValueError(f"max_depth must be between 1 and {MAX_TRAVERSAL_DEPTH}, got {max_depth}")
        starts = [u for u in dict.fromkeys(start_uris) if u]
        for u in starts:
            _iri(u)
        ns = self._ns(namespace)
        params: dict[str, Any] = {"ns": ns, "starts": json_text(starts), "max_depth": max_depth}
        edge = "e.namespace = :ns AND e.o_kind = 'iri' AND e.s_kind = 'iri'"
        if predicates:
            edge += f" AND e.p IN {_in_list('preds')}"
            params["preds"] = json_text(list(predicates))
        if ontology_id or graph_uri:
            edge += " AND e.graph_uri = :g"
            params["g"] = graph_uri or _ontology_graph_uri(str(ontology_id), ns)

        def typed(col: str) -> str:
            if not node_types:
                return ""
            params["ntype"] = _RDF_TYPE
            params["ntypes"] = json_text(list(node_types))
            return (
                f" AND EXISTS (SELECT 1 FROM coa_triple ty WHERE ty.graph_uri = e.graph_uri AND ty.s = {col} "
                f"AND ty.p = :ntype AND ty.o_kind = 'iri' AND ty.o IN {_in_list('ntypes')})"
            )

        parts = []
        if direction in ("out", "both"):
            parts.append(f"SELECT e.o AS nxt FROM coa_triple e WHERE {edge} AND e.s = w.uri{typed('e.o')}")
        if direction in ("in", "both"):
            parts.append(f"SELECT e.s AS nxt FROM coa_triple e WHERE {edge} AND e.o = w.uri{typed('e.s')}")
        neighbors = " UNION ".join(parts)
        if return_paths:
            walk = (
                "walk(start_uri, uri, depth, path) AS ("
                "SELECT uri, uri, 0, ARRAY[uri] FROM starts "
                "UNION ALL "
                "SELECT w.start_uri, n.nxt, w.depth + 1, w.path || n.nxt "
                f"FROM walk w CROSS JOIN LATERAL ({neighbors}) n "
                "WHERE w.depth < :max_depth AND n.nxt <> ALL (w.path))"
            )
        else:
            walk = (
                "walk(start_uri, uri, depth) AS ("
                "SELECT uri, uri, 0 FROM starts "
                "UNION "
                "SELECT w.start_uri, n.nxt, w.depth + 1 "
                f"FROM walk w CROSS JOIN LATERAL ({neighbors}) n "
                "WHERE w.depth < :max_depth)"
            )
        cte = (
            "WITH RECURSIVE starts AS "
            "(SELECT x.u AS uri FROM jsonb_array_elements_text(CAST(:starts AS jsonb)) AS x(u)), " + walk
        )
        return cte, params

    def traverse(
        self,
        start_uris: Sequence[str],
        *,
        predicates: Sequence[str] | None = None,
        direction: str = "out",
        max_depth: int,
        namespace: str | None = None,
        ontology_id: str | None = None,
        limit: int = 1000,
        return_paths: bool = False,
        node_types: Sequence[str] | None = None,
    ) -> list[dict]:
        """Multi-hop reachability over IRI-to-IRI edges with one recursive CTE.

        Args:
            start_uris: Vertices to start from (each must be a valid IRI).
            predicates: Only follow these predicates; all predicates when ``None``.
            direction: ``out`` (subject to object), ``in`` (object to subject) or ``both``.
            max_depth: Mandatory depth bound, 1 to :data:`MAX_TRAVERSAL_DEPTH`. Hops beyond it are not explored.
            namespace: Namespace to read; defaults to the store's.
            ontology_id: Restrict edges to one ontology's graph.
            limit: Maximum rows returned (at most :data:`MAX_TRAVERSAL_LIMIT`).
            return_paths: Return every distinct path (cycle-free) instead of the shortest depth per node.
            node_types: Only walk through nodes typed (``rdf:type``) as one of these in the edge's graph.

        Returns:
            Rows ``{"start", "uri", "depth"}`` ordered by depth then IRI; with ``return_paths`` each row also has
            ``"path"`` (list of IRIs from the start). The start vertex is never returned as its own descendant.
        """
        if not start_uris:
            return []
        cte, params = self._walk_cte(
            start_uris,
            predicates=predicates,
            direction=direction,
            max_depth=max_depth,
            namespace=namespace,
            ontology_id=ontology_id,
            return_paths=return_paths,
            node_types=node_types,
        )
        params["limit"] = max(1, min(limit, MAX_TRAVERSAL_LIMIT))
        if return_paths:
            sql = (
                f"{cte} SELECT start_uri AS start, uri, depth, to_jsonb(path)::text AS path FROM walk WHERE depth > 0 "
                'ORDER BY depth, start_uri COLLATE "C", path LIMIT :limit'
            )
        else:
            sql = (
                f"{cte} SELECT start_uri AS start, uri, min(depth) AS depth FROM walk "
                "WHERE depth > 0 AND uri <> start_uri GROUP BY start_uri, uri "
                'ORDER BY min(depth), start_uri COLLATE "C", uri COLLATE "C" LIMIT :limit'
            )
        rows = self._db().execute(sql, params)
        if return_paths:
            import json

            for r in rows:
                r["path"] = json.loads(r["path"])
        return rows

    def _descendant_counts(self, roots: Sequence[str], graph_uri_scope: str, namespace: str | None) -> dict[str, int]:
        """Number of distinct subclass descendants of each root inside one ontology graph (class nodes only)."""
        valid = [r for r in roots if _is_storable_iri(r)]
        if not valid:
            return {}
        db = self._db()
        out: dict[str, int] = {}
        ns = self._ns(namespace)
        for i in range(0, len(valid), _URI_CHUNK):
            cte, params = self._walk_cte(
                valid[i : i + _URI_CHUNK],
                predicates=[_SUBCLASS_OF],
                direction="in",
                max_depth=MAX_TRAVERSAL_DEPTH,
                namespace=ns,
                ontology_id=None,
                return_paths=False,
                node_types=_CLASS_TYPES,
                graph_uri=graph_uri_scope,
            )
            rows = db.execute(
                f"{cte} SELECT start_uri AS start, count(DISTINCT uri) AS n FROM walk "
                "WHERE depth > 0 AND uri <> start_uri GROUP BY start_uri",
                params,
            )
            out.update({r["start"]: int(r["n"]) for r in rows})
        return out

    # ── health ──────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Probe the database with ``SELECT 1``."""
        try:
            if self._client is None:
                self._client = default_client()
            self._client.execute("SELECT 1 AS ok")
            return {"status": "ok"}
        except AuroraConfigError:
            return {"status": "unconfigured"}
        except Exception as e:
            return {"status": "error", "error": str(e)}


__all__ = ["MAX_TRAVERSAL_DEPTH", "MAX_TRAVERSAL_LIMIT", "AuroraGraphStore"]
