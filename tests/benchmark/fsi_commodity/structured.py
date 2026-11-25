# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Structured tables behind the documents: CSV, Postgres DDL and seed data, and data-catalog fixtures.

Every row comes from the built scenarios and the universe, never retyped. The catalog fixtures have the
shape that ``packages/ontology-engine/demo/stage_fixtures.py`` writes (OpenMetadata-style ``databases``,
``databaseSchemas`` and ``tables`` plus a ``sample_data`` catalog document with a ``JDBC_DATABASE`` source).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .build import BuildResult

DB_ID = "db-fsi-001"
SCHEMA_ID = "schema-fsi-001"
SERVICE_ID = "svc-fsi-001"
DB_FQN = "fsi_postgres.fsi_commodity"
SCHEMA_NAME = "trading"
SCHEMA_FQN = f"{DB_FQN}.{SCHEMA_NAME}"
NAMESPACE_ID = "ns-fsi-commodity"
DATASOURCE_ID = "ds-fsi-commodity"
DATABASE_NAME = "fsi_commodity"
DATABASE_DESCRIPTION = (
    "NewCo commodity-trading tables behind the synthetic corpus: counterparties, accounts, credit limits, "
    "deals, invoices, payments and ownership edges. All values are invented."
)
STRUCTURED_DIR = "structured"
CATALOG_DIR = f"{STRUCTURED_DIR}/data-catalog"


@dataclass(frozen=True)
class Column:
    """One column: name, SQL type (``VARCHAR``, ``NUMERIC``, ``DATE``, ``INT`` or ``TEXT``) and its constraints."""

    name: str
    type: str
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    not_null: bool = False

    def sql_type(self) -> str:
        if self.type == "VARCHAR":
            return f"VARCHAR({self.length})"
        if self.type == "NUMERIC":
            return f"NUMERIC({self.precision},{self.scale})"
        return self.type


@dataclass(frozen=True)
class Table:
    """A table: columns, primary key, foreign keys ``(column, referred table, referred column)``."""

    name: str
    description: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[tuple[str, str, str], ...] = ()


def _v(name: str, length: int = 80, *, not_null: bool = False) -> Column:
    return Column(name, "VARCHAR", length=length, not_null=not_null)


def _n(name: str, scale: int = 2) -> Column:
    return Column(name, "NUMERIC", precision=20, scale=scale)


TABLES: tuple[Table, ...] = (
    Table(
        "counterparties",
        "Every legal entity in universe.json: NewCo entities, counterparties, holdings, banks, service parties.",
        (
            _v("org_id", 40, not_null=True),
            _v("name", 120, not_null=True),
            _v("legal_form", 40),
            _v("kind", 20, not_null=True),
            _v("country", 4),
            _v("lei", 20),
            _v("tax_id", 40),
            _v("jurisdiction", 60),
            Column("incorporation_date", "DATE"),
            _v("class_uri", 200),
        ),
        ("org_id",),
    ),
    Table(
        "accounts",
        "Bank accounts from universe.json with their identifiers.",
        (
            _v("account_id", 60, not_null=True),
            _v("owner_org_id", 40, not_null=True),
            _v("bank_org_id", 40),
            _v("currency", 3, not_null=True),
            _v("purpose", 30),
            _v("account_name", 120),
            _v("account_number", 40),
            _v("bic", 11),
            _v("iban", 34),
            _v("aba", 9),
            _v("bsb", 7),
            _v("class_uri", 200),
        ),
        ("account_id",),
        (("owner_org_id", "counterparties", "org_id"), ("bank_org_id", "counterparties", "org_id")),
    ),
    Table(
        "limits",
        "Credit limits and agreement terms approved in each onboarding scenario.",
        (
            _v("scenario_id", 60, not_null=True),
            _v("entity_org_id", 40, not_null=True),
            _v("counterparty_org_id", 40, not_null=True),
            _v("agreement", 200),
            _n("requested"),
            _n("unsecured"),
            _n("guaranteed"),
            _n("sblc"),
            _n("credit_limit"),
            _n("exposure"),
            _n("headroom"),
            Column("payment_days", "INT"),
            _v("governing_law", 80),
            Column("effective_date", "DATE"),
        ),
        ("scenario_id",),
        (("entity_org_id", "counterparties", "org_id"), ("counterparty_org_id", "counterparties", "org_id")),
    ),
    Table(
        "deals",
        "Every deal in the trade phase, with its credit check outcome.",
        (
            _v("trade_id", 30, not_null=True),
            _v("scenario_id", 60, not_null=True),
            _v("entity_org_id", 40, not_null=True),
            _v("counterparty_org_id", 40, not_null=True),
            _v("kind", 20),
            _v("side", 20),
            _v("product", 60),
            _n("quantity", 3),
            _v("unit", 10),
            _v("currency", 3),
            _n("notional"),
            _v("status", 20),
            _v("credit_status", 30),
            _v("terms_json", 2000),
        ),
        ("trade_id",),
        (("entity_org_id", "counterparties", "org_id"), ("counterparty_org_id", "counterparties", "org_id")),
    ),
    Table(
        "invoices",
        "The last cash-bearing post-trade document of each trade (invoice, credit note, margin call, confirmation).",
        (
            _v("document_number", 60, not_null=True),
            _v("trade_id", 30, not_null=True),
            _v("kind", 30),
            _v("issuer_org_id", 40),
            _v("payer_org_id", 40),
            _v("payee_org_id", 40),
            _v("currency", 3),
            _n("payable"),
            Column("due_date", "DATE"),
            _v("payer_account_id", 60),
            _v("payee_account_id", 60),
            _v("filename", 120),
        ),
        ("document_number",),
        (
            ("trade_id", "deals", "trade_id"),
            ("issuer_org_id", "counterparties", "org_id"),
            ("payer_org_id", "counterparties", "org_id"),
            ("payee_org_id", "counterparties", "org_id"),
            ("payer_account_id", "accounts", "account_id"),
            ("payee_account_id", "accounts", "account_id"),
        ),
    ),
    Table(
        "payments",
        "Cash movements from the settlement phase.",
        (
            _v("payment_id", 70, not_null=True),
            _v("trade_id", 30, not_null=True),
            _v("scenario_id", 60, not_null=True),
            _n("amount", 2),
            _v("currency", 3),
            Column("value_date", "DATE"),
            Column("due_date", "DATE"),
            Column("days_late", "INT"),
            _n("interest"),
            _n("collateral"),
            _v("status", 30),
        ),
        ("payment_id",),
        (("trade_id", "deals", "trade_id"),),
    ),
    Table(
        "ownership_edges",
        "Ownership edges: group structure from the universe and the per-scenario edges found in onboarding.",
        (
            _v("edge_id", 20, not_null=True),
            _v("scenario_id", 60),
            _v("parent_id", 40, not_null=True),
            _v("child_id", 40, not_null=True),
            Column("percent", "NUMERIC", precision=7, scale=3),
            _v("relationship", 40),
            _v("class_uri", 200),
        ),
        ("edge_id",),
    ),
)

TABLES_BY_NAME = {t.name: t for t in TABLES}


# --- rows ---------------------------------------------------------------------------------------


def _table_rows(result: BuildResult) -> dict[str, list[dict[str, Any]]]:
    u = result.universe
    orgs: Mapping[str, Mapping[str, Any]] = u["orgs"]
    accounts: Mapping[str, Mapping[str, Any]] = u["accounts"]
    rows: dict[str, list[dict[str, Any]]] = {t.name: [] for t in TABLES}

    for org_id in sorted(orgs):
        o = orgs[org_id]
        inc = o.get("incorporation") or {}
        rows["counterparties"].append(
            {
                "org_id": org_id,
                "name": o["name"],
                "legal_form": o.get("legal_form"),
                "kind": o["kind"],
                "country": o.get("country"),
                "lei": o.get("lei"),
                "tax_id": o.get("tax_id"),
                "jurisdiction": inc.get("jurisdiction"),
                "incorporation_date": inc.get("date"),
                "class_uri": o.get("class_uri"),
            }
        )
    for acct_id in sorted(accounts):
        a = accounts[acct_id]
        rows["accounts"].append(
            {
                "account_id": acct_id,
                "owner_org_id": a["owner"],
                "bank_org_id": a.get("bank"),
                "currency": a["currency"],
                "purpose": a.get("purpose"),
                "account_name": a.get("name"),
                "account_number": a.get("number"),
                "bic": a.get("bic"),
                "iban": a.get("iban"),
                "aba": a.get("aba"),
                "bsb": a.get("bsb"),
                "class_uri": a.get("class_uri"),
            }
        )

    deal_ids: set[str] = set()
    edge_no = 0

    def add_edge(scenario_id: str | None, e: Mapping[str, Any]) -> None:
        nonlocal edge_no
        edge_no += 1
        rows["ownership_edges"].append(
            {
                "edge_id": f"edge-{edge_no:03d}",
                "scenario_id": scenario_id,
                "parent_id": e["parent"],
                "child_id": e["child"],
                "percent": e.get("percent"),
                "relationship": e.get("relationship"),
                "class_uri": e.get("class_uri"),
            }
        )

    for e in u["ownership_edges"]:
        add_edge(None, e)

    for sc in result.scenarios:
        if sc.phase == "onboarding":
            ob = sc.data.get("onboarded")
            if ob is None:
                continue
            credit = ob["credit"]
            rows["limits"].append(
                {
                    "scenario_id": sc.id,
                    "entity_org_id": ob["entity"],
                    "counterparty_org_id": ob["counterparty"],
                    "agreement": ob.get("agreement") or None,
                    "requested": credit.get("requested"),
                    "unsecured": credit.get("unsecured"),
                    "guaranteed": credit.get("guaranteed"),
                    "sblc": credit.get("sblc"),
                    "credit_limit": credit.get("limit"),
                    "exposure": credit.get("exposure"),
                    "headroom": credit.get("headroom"),
                    "payment_days": ob.get("payment_days"),
                    "governing_law": ob.get("governing_law"),
                    "effective_date": ob.get("effective"),
                }
            )
            for e in sc.ownership_edges:
                add_edge(sc.id, e)
        elif sc.phase == "trade":
            for d in sc.data.get("deals", []):
                t = d["terms"]
                deal_ids.add(d["trade_id"])
                credit_check = d.get("credit_check") or {}
                rows["deals"].append(
                    {
                        "trade_id": d["trade_id"],
                        "scenario_id": sc.id,
                        "entity_org_id": d["entity"],
                        "counterparty_org_id": d["counterparty"],
                        "kind": d.get("kind"),
                        "side": d.get("side"),
                        "product": t.get("product"),
                        "quantity": t.get("quantity"),
                        "unit": t.get("unit"),
                        "currency": t.get("currency"),
                        "notional": d.get("notional"),
                        "status": d.get("status"),
                        "credit_status": credit_check.get("status"),
                        "terms_json": json.dumps(t, sort_keys=True, ensure_ascii=False),
                    }
                )
        elif sc.phase == "post_trade":
            for doc in sc.data.get("documents", []):
                rows["invoices"].append(
                    {
                        "document_number": doc["number"],
                        "trade_id": doc["trade_id"],
                        "kind": doc["kind"],
                        "issuer_org_id": doc.get("issuer"),
                        "payer_org_id": doc.get("payer"),
                        "payee_org_id": doc.get("payee"),
                        "currency": doc.get("currency"),
                        "payable": doc.get("payable"),
                        "due_date": doc.get("due"),
                        "payer_account_id": doc["payer_account"] if doc.get("payer_account") in accounts else None,
                        "payee_account_id": doc["payee_account"] if doc.get("payee_account") in accounts else None,
                        "filename": doc.get("filename"),
                    }
                )
        elif sc.phase == "settlement":
            for p in sc.data.get("payments", []):
                rows["payments"].append(
                    {
                        "payment_id": f"pay-{sc.id}",
                        "trade_id": p["trade_id"],
                        "scenario_id": sc.id,
                        "amount": p.get("amount"),
                        "currency": p.get("currency"),
                        "value_date": p.get("value_date"),
                        "due_date": p.get("due"),
                        "days_late": p.get("days_late"),
                        "interest": p.get("interest"),
                        "collateral": p.get("collateral"),
                        "status": _payment_status(p),
                    }
                )
    return rows


def _payment_status(p: Mapping[str, Any]) -> str:
    if p.get("days_late"):
        return "paid-late"
    if p.get("top_up"):
        return "short-paid-then-topped-up"
    return "paid"


# --- serialisation ------------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Render a cell for CSV and SQL: plain decimals, no exponent, empty for ``None`` is handled by callers."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    return str(value)


def render_csv(table: Table, rows: Sequence[Mapping[str, Any]]) -> bytes:
    """Header plus rows; ``None`` is an empty cell; ``\\n`` line endings."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([c.name for c in table.columns])
    for row in rows:
        writer.writerow(["" if row[c.name] is None else _text(row[c.name]) for c in table.columns])
    return buf.getvalue().encode("utf-8")


def _sql_literal(column: Column, value: Any) -> str:
    if value is None:
        return "NULL"
    if column.type in ("NUMERIC", "INT"):
        return _text(value)
    return "'" + _text(value).replace("'", "''") + "'"


def render_schema(tables: Sequence[Table] = TABLES) -> bytes:
    """Postgres DDL with primary keys and foreign keys, parents before children."""
    lines = [
        "-- NewCo commodity-trading corpus: structured tables (Postgres). Generated; do not edit.",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};",
        "",
    ]
    for t in tables:
        lines.append(f"-- {t.description}")
        lines.append(f"CREATE TABLE {SCHEMA_NAME}.{t.name} (")
        parts = [f"    {c.name} {c.sql_type()}{' NOT NULL' if c.not_null else ''}" for c in t.columns]
        parts.append(f"    PRIMARY KEY ({', '.join(t.primary_key)})")
        parts.extend(
            f"    FOREIGN KEY ({col}) REFERENCES {SCHEMA_NAME}.{ref_table} ({ref_col})"
            for col, ref_table, ref_col in t.foreign_keys
        )
        lines.append(",\n".join(parts))
        lines.append(");")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def render_seed(rows: Mapping[str, Sequence[Mapping[str, Any]]], tables: Sequence[Table] = TABLES) -> bytes:
    """One INSERT per row, in table order, values SQL-escaped."""
    lines = ["-- NewCo commodity-trading corpus: seed data. Generated from the same rows as the CSV files.", ""]
    for t in tables:
        names = ", ".join(c.name for c in t.columns)
        for row in rows[t.name]:
            values = ", ".join(_sql_literal(c, row[c.name]) for c in t.columns)
            lines.append(f"INSERT INTO {SCHEMA_NAME}.{t.name} ({names}) VALUES ({values});")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


# --- data-catalog fixtures (shape of stage_fixtures.py) -----------------------------------------


def _json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _catalog_columns(table: Table) -> list[dict[str, Any]]:
    out = []
    for i, col in enumerate(table.columns, 1):
        c: dict[str, Any] = {"name": col.name, "dataType": col.type, "ordinalPosition": i}
        if col.length is not None:
            c["dataLength"] = col.length
        if col.precision is not None:
            c["precision"] = col.precision
            c["scale"] = col.scale
        if col.not_null:
            c["constraint"] = "NOT_NULL"
        out.append(c)
    return out


def _catalog_constraints(table: Table) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = [{"constraintType": "PRIMARY_KEY", "columns": list(table.primary_key)}]
    for col, ref_table, ref_col in table.foreign_keys:
        constraints.append(
            {
                "constraintType": "FOREIGN_KEY",
                "columns": [col],
                "referredColumns": [f"{SCHEMA_FQN}.{ref_table}.{ref_col}"],
            }
        )
    return constraints


def catalog_fixtures(tables: Sequence[Table] = TABLES) -> dict[str, Any]:
    """Return ``databases``, ``databaseSchemas``, ``tables`` (lists) and ``sample_data`` (the catalog document)."""
    database = {
        "id": DB_ID,
        "name": DATABASE_NAME,
        "displayName": "NewCo Commodity Trading Database",
        "fullyQualifiedName": DB_FQN,
        "description": "",
        "service": {
            "id": SERVICE_ID,
            "type": "databaseService",
            "name": "fsi_postgres",
            "fullyQualifiedName": "fsi_postgres",
            "serviceType": "Postgres",
        },
        "tags": [],
        "owners": [],
    }
    schema = {
        "id": SCHEMA_ID,
        "name": SCHEMA_NAME,
        "displayName": "Trading Schema",
        "fullyQualifiedName": SCHEMA_FQN,
        "description": "",
        "database": {"id": DB_ID, "type": "database", "name": DATABASE_NAME, "fullyQualifiedName": DB_FQN},
    }
    table_entries = [
        {
            "id": f"fsi-{i:03d}",
            "name": t.name,
            "fullyQualifiedName": f"{SCHEMA_FQN}.{t.name}",
            "tableType": "Regular",
            "databaseSchema": {
                "id": SCHEMA_ID,
                "type": "databaseSchema",
                "name": SCHEMA_NAME,
                "fullyQualifiedName": SCHEMA_FQN,
            },
            "columns": _catalog_columns(t),
            "tableConstraints": _catalog_constraints(t),
            "tags": [],
        }
        for i, t in enumerate(tables, 1)
    ]
    source_tables = []
    for t in tables:
        fks = [
            {
                "column": col,
                "targetTable": ref_table,
                "targetColumn": ref_col,
                "source": "DETERMINISTIC",
                "confidence": 1.0,
            }
            for col, ref_table, ref_col in t.foreign_keys
        ]
        source_tables.append(
            {
                "name": t.name,
                "technicalMetadata": {"columnCount": len(t.columns), "format": "postgres"},
                "businessMetadata": {
                    "description": t.description,
                    "enrichmentSource": "DETERMINISTIC",
                    "reviewStatus": "APPROVED",
                },
                "primaryKey": {"columns": list(t.primary_key), "source": "DETERMINISTIC", "confidence": 1.0},
                "foreignKeys": fks,
                "columns": [
                    {
                        "name": c.name,
                        "dataType": c.type,
                        "description": "",
                        "constraint": "NOT_NULL" if c.not_null else None,
                    }
                    for c in t.columns
                ],
            }
        )
    sample = {
        "namespaceId": NAMESPACE_ID,
        "sources": [
            {
                "datasourceId": DATASOURCE_ID,
                "sourceType": "JDBC_DATABASE",
                "databases": [{"name": DATABASE_NAME, "description": DATABASE_DESCRIPTION, "tables": source_tables}],
            }
        ],
    }
    return {
        "databases": [database],
        "databaseSchemas": [schema],
        "tables": table_entries,
        "sample_data": sample,
    }


def render_structured(result: BuildResult) -> dict[str, bytes]:
    """Return every structured file, keyed by path relative to ``corpus/``."""
    rows = _table_rows(result)
    files: dict[str, bytes] = {}
    for t in TABLES:
        files[f"{STRUCTURED_DIR}/{t.name}.csv"] = render_csv(t, rows[t.name])
    files[f"{STRUCTURED_DIR}/schema.sql"] = render_schema()
    files[f"{STRUCTURED_DIR}/seed.sql"] = render_seed(rows)
    fixtures = catalog_fixtures()
    for name in ("databases", "databaseSchemas", "tables"):
        files[f"{CATALOG_DIR}/{name}.json"] = _json(fixtures[name])
    files[f"{CATALOG_DIR}/sample_data/fsi_commodity.json"] = _json(fixtures["sample_data"])
    return files


def table_rows(result: BuildResult) -> dict[str, list[dict[str, Any]]]:
    """Return the rows per table (used by tests and golden)."""
    return _table_rows(result)
