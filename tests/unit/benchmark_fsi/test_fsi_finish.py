# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Finish phase: structured tables, golden answers, replay, ``--check`` and whole-corpus totals."""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.benchmark.fsi_commodity import build as build_mod
from tests.benchmark.fsi_commodity.build import CORPUS_DIR, BuildResult, build, render_files, write_files
from tests.benchmark.fsi_commodity.check import diff_corpus
from tests.benchmark.fsi_commodity.cli import main
from tests.benchmark.fsi_commodity.golden import GOLDEN_PATH, QUESTION_TYPES, build_golden, render_golden
from tests.benchmark.fsi_commodity.replay import main as replay_main
from tests.benchmark.fsi_commodity.structured import TABLES, catalog_fixtures, table_rows
from tests.unit.benchmark_fsi.test_fsi_ids import valid_aba, valid_abn, valid_bic, valid_iban, valid_lei

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
PACK = REPO / "packages" / "ontology-engine" / "demo" / "config-fsi-commodity.yaml"
STAGE_FIXTURES = REPO / "packages" / "ontology-engine" / "demo" / "stage_fixtures.py"


@pytest.fixture(scope="module")
def result() -> BuildResult:
    return build()


@pytest.fixture(scope="module")
def files(result: BuildResult) -> dict[str, bytes]:
    return render_files(result)


def _pack_uris() -> set[str]:
    with PACK.open(encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    uris: set[str] = set()
    for group in ("foundational_ontologies", "domain_ontologies", "customer_ontologies"):
        for onto in pack.get(group) or []:
            uris.update(c["class_uri"] for c in onto.get("sample_classes") or [])
    return uris


# --- totals -------------------------------------------------------------------------------------


def test_scenario_and_phase_totals_match_the_brief(result: BuildResult) -> None:
    # BRIEF.md's 24 scenarios plus the docsets (2), lifecycle (1) and execution (1) phases.
    assert len(result.scenarios) == 28
    per_phase: dict[str, list[int]] = {}
    for s in result.scenarios:
        c = s.counts
        acc = per_phase.setdefault(s.phase, [0, 0, 0, 0])
        acc[0] += 1
        acc[1] += c["emails"]
        acc[2] += c["attachments"]
        acc[3] += c["chats"]
    assert per_phase == {
        "onboarding": [6, 138, 95, 0],
        "trade": [6, 42, 18, 13],
        "post_trade": [6, 55, 39, 0],
        "settlement": [6, 34, 19, 0],
        "docsets": [2, 8, 14, 0],
        "lifecycle": [1, 26, 17, 0],
        "execution": [1, 8, 6, 18],
    }
    assert sum(s.counts["emails"] for s in result.scenarios) == 311
    assert sum(s.counts["attachments"] for s in result.scenarios) == 208
    assert sum(s.counts["chats"] for s in result.scenarios) == 31
    assert sum(len(s.data["deals"]) for s in result.scenarios if s.phase == "trade") == 7


def test_committed_corpus_has_the_brief_counts() -> None:
    manifests = [json.loads(p.read_text(encoding="utf-8")) for p in CORPUS_DIR.glob("*/*/manifest.json")]
    assert len(manifests) == 28
    assert sum(m["counts"]["emails"] for m in manifests) == 311
    assert sum(m["counts"]["attachments"] for m in manifests) == 208
    assert sum(m["counts"]["chats"] for m in manifests) == 31
    assert len(list(CORPUS_DIR.glob("*/*/messages/*.eml"))) == 311
    assert len(list(CORPUS_DIR.glob("*/*/messages/*.md"))) == 311
    assert len(list(CORPUS_DIR.glob("*/*/chats/*.json"))) == 31
    assert len(list(CORPUS_DIR.glob("*/*/attachments/*"))) == 208


# --- the committed corpus against the brief's rules ---------------------------------------------


def test_committed_universe_identifiers_pass_their_checksums() -> None:
    u = json.loads((CORPUS_DIR / "universe.json").read_text(encoding="utf-8"))
    leis = [o["lei"] for o in u["orgs"].values() if o.get("lei")]
    assert leis
    assert all(v.startswith("SYNT") and valid_lei(v) for v in leis)
    for a in u["accounts"].values():
        if a.get("iban"):
            assert valid_iban(a["iban"])
        if a.get("aba"):
            assert valid_aba(a["aba"])
        if a.get("bic"):
            assert valid_bic(a["bic"])
    abns = [
        o["tax_id"].removeprefix("ABN ").replace(" ", "")
        for o in u["orgs"].values()
        if "ABN" in (o.get("tax_id") or "")
    ]
    assert abns
    assert all(valid_abn(v) for v in abns)


def _class_values(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, val in node.items():
            if key.endswith("class_uri") and isinstance(val, str):
                out.append(val)
            elif key == "class_uris" and isinstance(val, dict):
                out.extend(v for v in val.values() if isinstance(v, str))
            else:
                out.extend(_class_values(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_class_values(item))
    return out


def test_every_class_uri_on_disk_is_in_sample_classes() -> None:
    allowed = _pack_uris()
    seen: set[str] = set()
    for path in [CORPUS_DIR / "universe.json", *CORPUS_DIR.glob("*/*/manifest.json")]:
        seen.update(_class_values(json.loads(path.read_text(encoding="utf-8"))))
    assert seen
    assert seen <= allowed, sorted(seen - allowed)


def test_every_planted_problem_is_recorded_in_the_manifests() -> None:
    for path in CORPUS_DIR.glob("*/*/manifest.json"):
        m = json.loads(path.read_text(encoding="utf-8"))
        contradictions = {c["id"] for c in m["expected_contradictions"]}
        for deal in m["deals"]:
            for mm in deal["mismatches"]:
                assert mm["id"] in contradictions, (m["scenario"], mm["id"])


# --- structured ---------------------------------------------------------------------------------


def test_csv_rows_match_the_tables_and_schema(files: dict[str, bytes], result: BuildResult) -> None:
    rows = table_rows(result)
    schema = files["structured/schema.sql"].decode()
    seed = files["structured/seed.sql"].decode()
    for t in TABLES:
        parsed = list(csv.reader(io.StringIO(files[f"structured/{t.name}.csv"].decode())))
        assert parsed[0] == [c.name for c in t.columns]
        assert len(parsed) - 1 == len(rows[t.name]) > 0
        assert f"CREATE TABLE trading.{t.name} (" in schema
        assert seed.count(f"INSERT INTO trading.{t.name} (") == len(rows[t.name])
    assert len(rows["deals"]) == 7
    assert len(rows["payments"]) == 6
    assert len(rows["limits"]) == 6


def test_seed_sql_loads_into_sqlite(files: dict[str, bytes], result: BuildResult) -> None:
    ddl = files["structured/schema.sql"].decode()
    ddl = ddl.replace("CREATE SCHEMA IF NOT EXISTS trading;", "").replace("trading.", "")
    ddl = re.sub(r"\b(VARCHAR|NUMERIC)\(\d+(,\d+)?\)", lambda m: "TEXT" if m.group(1) == "VARCHAR" else "NUMERIC", ddl)
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(ddl)
    conn.executescript(files["structured/seed.sql"].decode().replace("trading.", ""))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    rows = table_rows(result)
    for t in TABLES:
        assert conn.execute(f"SELECT COUNT(*) FROM {t.name}").fetchone()[0] == len(rows[t.name])
    (total,) = conn.execute("SELECT amount FROM payments WHERE trade_id = 'NC-HOU-26-0001'").fetchone()
    assert abs(total - 16805084.20) < 0.005


def test_seed_sql_escapes_quotes() -> None:
    from tests.benchmark.fsi_commodity.structured import Column, render_seed

    table = type(TABLES[0])("t", "d", (Column("a", "VARCHAR", length=20),), ("a",))
    seed = render_seed({"t": [{"a": "O'Neil"}]}, (table,)).decode()
    assert "'O''Neil'" in seed


def test_catalog_fixtures_have_the_stage_fixtures_shape() -> None:
    fx = catalog_fixtures()
    src = STAGE_FIXTURES.read_text(encoding="utf-8")
    # Keys stage_fixtures.py writes; each must appear as a literal key there and in our output.
    for key in (
        "id",
        "name",
        "fullyQualifiedName",
        "tableType",
        "databaseSchema",
        "columns",
        "tableConstraints",
        "tags",
    ):
        assert f'"{key}"' in src
        assert all(key in t for t in fx["tables"])
    for key in ("displayName", "service", "owners", "description"):
        assert key in fx["databases"][0]
    assert {"id", "database", "fullyQualifiedName"} <= set(fx["databaseSchemas"][0])
    sample = fx["sample_data"]
    assert set(sample) == {"namespaceId", "sources"}
    source = sample["sources"][0]
    assert source["sourceType"] == "JDBC_DATABASE"
    table = source["databases"][0]["tables"][0]
    assert set(table) == {"name", "technicalMetadata", "businessMetadata", "primaryKey", "foreignKeys", "columns"}
    assert fx["databases"][0]["id"].startswith("db-fsi-")
    assert [t["id"] for t in fx["tables"]] == [f"fsi-{i:03d}" for i in range(1, len(TABLES) + 1)]


def test_structured_files_are_in_the_corpus(files: dict[str, bytes]) -> None:
    expected = {f"structured/{t.name}.csv" for t in TABLES} | {
        "structured/schema.sql",
        "structured/seed.sql",
        "structured/data-catalog/databases.json",
        "structured/data-catalog/databaseSchemas.json",
        "structured/data-catalog/tables.json",
        "structured/data-catalog/sample_data/fsi_commodity.json",
    }
    assert expected <= set(files)
    assert all((CORPUS_DIR / rel).is_file() for rel in expected)


# --- golden -------------------------------------------------------------------------------------


def test_golden_format_and_question_types(result: BuildResult) -> None:
    golden = build_golden(result)
    comments = {k for k in golden if k.startswith("//")}
    assert comments and all(isinstance(golden[k], str) for k in comments)
    queries = {k: v for k, v in golden.items() if k not in comments}
    assert len(queries) >= 72
    for qid, entry in queries.items():
        assert isinstance(entry["expected"], int), qid
        assert entry["tolerance_pct"] == 0
        assert entry["tolerance"] == 0
        assert entry["type"] in QUESTION_TYPES
        assert "answer" in entry
    assert {e["type"] for e in queries.values()} == set(QUESTION_TYPES)


def test_golden_file_is_current_and_compare_py_can_read_it(result: BuildResult) -> None:
    assert GOLDEN_PATH.read_bytes() == render_golden(result)
    from tests.benchmark.compare import is_correct

    golden = {k: v for k, v in json.loads(GOLDEN_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    qid = "s1-houston-vitol-wti-q1"
    assert is_correct({"status": 200, "body": {"results": [{"limit": 40000000}]}}, golden[qid])
    assert not is_correct({"status": 200, "body": {"results": []}}, golden[qid])


# --- replay and --check -------------------------------------------------------------------------


def test_replay_runs_offline_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert replay_main([]) == 0
    out = capsys.readouterr().out
    assert "observed totals: 311 emails, 208 attachments, 31 chats" in out


def test_replay_fails_when_an_attachment_changes(tmp_path: Path, files: dict[str, bytes]) -> None:
    write_files(files, tmp_path)
    victim = next(tmp_path.glob("onboarding/*/attachments/*.pdf"))
    victim.write_bytes(victim.read_bytes() + b"x")
    assert replay_main(["--corpus", str(tmp_path)]) == 1


def test_check_exits_zero_on_the_committed_corpus() -> None:
    assert main(["--check"]) == 0


def test_check_fails_after_a_corpus_file_changes(tmp_path: Path, files: dict[str, bytes]) -> None:
    write_files(files, tmp_path)
    assert main(["--check", "--out", str(tmp_path)]) == 0
    (tmp_path / "structured" / "deals.csv").write_bytes(b"altered\n")
    assert main(["--check", "--out", str(tmp_path)]) == 1
    shutil.copyfile(CORPUS_DIR / "structured" / "deals.csv", tmp_path / "structured" / "deals.csv")
    (tmp_path / "structured" / "stray.csv").write_bytes(b"x\n")
    assert diff_corpus(files, tmp_path)["extra"] == ["structured/stray.csv"]


def test_build_twice_is_byte_identical(files: dict[str, bytes]) -> None:
    again = render_files(build_mod.build())
    assert again.keys() == files.keys()
    assert all(again[k] == files[k] for k in files)
