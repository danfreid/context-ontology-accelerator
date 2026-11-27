# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AuroraSparql (load, query, update diff) against a real Postgres holding ``coa_triple``.

Same setup as ``test_aurora_search_pg.py`` (it supplies ``PgDataApi`` and the init script):
    docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg16
    COA_PG_TEST_DSN=postgresql://postgres:pw@localhost:5432/postgres pytest -m integ libs/common/tests/integ -k sparql
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from coa_common import aurora_sparql
from coa_common.aurora_sparql import AuroraSparql, AuroraSparqlError

pytestmark = pytest.mark.integ

# Importing this module applies its skip when COA_PG_TEST_DSN or psycopg is missing.
from test_aurora_search_pg import DSN, PgDataApi, _init_sql, psycopg  # noqa: E402

RUN = uuid.uuid4().hex[:8]
BASE = f"https://sparql-{RUN}.test"
G1 = f"{BASE}/ns1/onto-a"
G2 = f"{BASE}/ns1/onto-b"
G_PCT = f"{BASE}/100%_ns/onto"  # a prefix with LIKE metacharacters
G_OTHER = f"{BASE}/ns2/onto-a"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    with psycopg.connect(DSN) as c:
        c.execute(_init_sql())
        c.commit()
        yield c
        c.execute("DELETE FROM coa_triple WHERE graph_uri LIKE %s", (f"{BASE}/%",))
        c.commit()


@pytest.fixture
def sparql(conn, monkeypatch) -> AuroraSparql:
    monkeypatch.setenv("NDB_GRAPH_URI_BASE", BASE)
    conn.execute("DELETE FROM coa_triple WHERE graph_uri LIKE %s", (f"{BASE}/%",))
    conn.commit()
    return AuroraSparql(PgDataApi(conn), ttl_s=60)


def _rows(conn: Any, graph: str) -> set[tuple[str, str, str, str, str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT s, p, o, o_kind, o_lang, o_datatype FROM coa_triple WHERE graph_uri = %s", (graph,))
        return set(cur.fetchall())


def _seed(sparql: AuroraSparql, graph: str, n: int, label: str = "Thing") -> None:
    triples = " ".join(f'<http://ex.org/{label}{i}> a <{OWL_CLASS}> ; <{RDFS_LABEL}> "{label} {i}" .' for i in range(n))
    sparql.update(f"INSERT DATA {{ GRAPH <{graph}> {{ {triples} }} }}")


def test_update_inserts_rows_with_the_namespace_from_the_graph_base(sparql, conn):
    _seed(sparql, G1, 2)
    assert len(_rows(conn, G1)) == 4
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT namespace FROM coa_triple WHERE graph_uri = %s", (G1,))
        assert cur.fetchall() == [("ns1",)]


def test_exact_graph_load_pages_by_keyset(sparql, conn, monkeypatch):
    _seed(sparql, G1, 7)  # 14 rows
    _seed(sparql, G2, 3, "Other")
    monkeypatch.setattr(aurora_sparql, "_PAGE_SIZE", 3)
    fresh = AuroraSparql(PgDataApi(conn), ttl_s=0)
    rows = fresh.query(f"SELECT ?c WHERE {{ GRAPH <{G1}> {{ ?c a <{OWL_CLASS}> }} }} ORDER BY ?c")
    assert [r["c"] for r in rows] == [f"http://ex.org/Thing{i}" for i in range(7)]


def test_prefix_load_is_namespace_scoped_and_escapes_like_metacharacters(sparql, conn):
    _seed(sparql, G1, 1, "A")
    _seed(sparql, G2, 1, "B")
    _seed(sparql, G_OTHER, 1, "C")
    _seed(sparql, G_PCT, 1, "D")

    def q(prefix: str) -> str:
        return (
            f"SELECT ?c WHERE {{ GRAPH ?g {{ ?c a <{OWL_CLASS}> }} "
            f'FILTER(STRSTARTS(STR(?g), "{prefix}")) }} ORDER BY ?c'
        )

    assert [r["c"] for r in sparql.query(q(f"{BASE}/ns1/"))] == ["http://ex.org/A0", "http://ex.org/B0"]
    # '%' and '_' in the prefix are literal, not wildcards
    assert [r["c"] for r in sparql.query(q(f"{BASE}/100%_ns/"))] == ["http://ex.org/D0"]
    assert sparql.query(q(f"{BASE}/100x_ns/")) == []
    assert sparql.query(q(f"{BASE}/1%0")) == []


def test_modify_diff_deletes_and_inserts_only_the_difference(sparql, conn):
    _seed(sparql, G1, 3)
    before = _rows(conn, G1)
    result = sparql.update(
        f"WITH <{G1}> DELETE {{ <http://ex.org/Thing1> <{RDFS_LABEL}> ?l }} "
        f'INSERT {{ <http://ex.org/Thing1> <{RDFS_LABEL}> "Renamed" }} '
        f"WHERE {{ <http://ex.org/Thing1> <{RDFS_LABEL}> ?l }}"
    )
    assert result == {"inserted": 1, "deleted": 1}
    after = _rows(conn, G1)
    assert after - before == {("http://ex.org/Thing1", RDFS_LABEL, "Renamed", "literal", "", "")}
    assert before - after == {("http://ex.org/Thing1", RDFS_LABEL, "Thing 1", "literal", "", "")}
    # a re-run changes nothing
    assert sparql.update(f'INSERT DATA {{ GRAPH <{G1}> {{ <http://ex.org/Thing1> <{RDFS_LABEL}> "Renamed" }} }}') == {
        "inserted": 0,
        "deleted": 0,
    }


def test_row_semantics_match_the_graph_store(sparql, conn):
    sparql.update(
        f"INSERT DATA {{ GRAPH <{G1}> {{ "
        f'<http://ex.org/A> <{RDFS_LABEL}> "plain", "typed"^^<http://www.w3.org/2001/XMLSchema#string>, '
        f'"deutsch"@de, "7"^^<http://www.w3.org/2001/XMLSchema#integer> ; '
        f"a <{OWL_CLASS}> . }} }}"
    )
    assert _rows(conn, G1) == {
        ("http://ex.org/A", RDF_TYPE, OWL_CLASS, "iri", "", ""),
        ("http://ex.org/A", RDFS_LABEL, "plain", "literal", "", ""),
        ("http://ex.org/A", RDFS_LABEL, "typed", "literal", "", ""),  # xsd:string is stored plain
        ("http://ex.org/A", RDFS_LABEL, "deutsch", "literal", "de", ""),
        ("http://ex.org/A", RDFS_LABEL, "7", "literal", "", "http://www.w3.org/2001/XMLSchema#integer"),
    }


def test_delete_where_drops_a_whole_graph_only(sparql, conn):
    _seed(sparql, G1, 2)
    _seed(sparql, G2, 2, "Keep")
    result = sparql.update(f"DELETE WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")
    assert result["deleted"] == 4
    assert _rows(conn, G1) == set()
    assert len(_rows(conn, G2)) == 4


def test_failed_update_leaves_no_rows_and_the_cache_is_dropped(sparql, conn):
    _seed(sparql, G1, 1)
    sparql.ask(f"ASK {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")  # caches G1
    with pytest.raises(AuroraSparqlError):
        sparql.update("INSERT DATA { nonsense")
    assert len(_rows(conn, G1)) == 2
    sparql.update(f"DELETE WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}")
    assert sparql.ask(f"ASK {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}") is False  # not served stale


def test_concurrent_updates_to_one_graph_are_serialised(sparql, conn):
    _seed(sparql, G1, 1)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            with psycopg.connect(DSN) as c:
                AuroraSparql(PgDataApi(c), ttl_s=0).update(
                    f'INSERT DATA {{ GRAPH <{G1}> {{ <http://ex.org/W{i}> <{RDFS_LABEL}> "w{i}" }} }}'
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    labels = {s for (s, p, *_rest) in _rows(conn, G1) if p == RDFS_LABEL}
    assert {f"http://ex.org/W{i}" for i in range(6)} <= labels


def test_turtle_round_trip(sparql, conn):
    _seed(sparql, G1, 2)
    ttl = sparql.turtle(G1)
    assert ttl is not None and "Thing 1" in ttl
    assert sparql.turtle(G2) is None
