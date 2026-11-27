# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The ETRM as a second structured source: a RightAngle-style deal capture database.

The ETRM is its own system, so it is its own datasource (``ds-etrm-rightangle``, schema ``rightangle``), separate
from the ``trading`` source in ``structured.py``. Deals are versioned: an amendment adds a row with the next
``version`` and keeps the old one, as the ETRM's deal history does. ``external_ref`` holds NewCo's trade id from
the email confirmation, which is the binding record the bookings are reconciled against.

Rows come from the execution phase's ground truth (``Scenario.execution["etrm_rows"]``), never retyped. Files are
written under ``corpus/structured/etrm/`` in the same shapes as the ``trading`` source: CSV per table,
``schema.sql`` (portable DDL; loads into Postgres for tests), ``seed.sql`` and data-catalog fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .structured import Column, Table, _sql_literal, render_csv

if TYPE_CHECKING:
    from .build import BuildResult

SCHEMA_NAME = "rightangle"
DATABASE_NAME = "rightangle"
DB_ID = "db-etrm-001"
SCHEMA_ID = "schema-etrm-001"
SERVICE_ID = "svc-etrm-001"
DB_FQN = "etrm_sqlserver.rightangle"
SCHEMA_FQN = f"{DB_FQN}.{SCHEMA_NAME}"
DATASOURCE_ID = "ds-etrm-rightangle"
NAMESPACE_ID = "ns-fsi-commodity"
ETRM_DIR = "structured/etrm"
CATALOG_DIR = f"{ETRM_DIR}/data-catalog"
DATABASE_DESCRIPTION = (
    "RightAngle-style deal capture for the NewCo Houston and London desks: business associates, products, and "
    "versioned deal headers and details as booked by the traders. All values are invented."
)
INTERNAL_BA = {"NEWCOHOU": "newco-hou", "NEWCOLON": "newco-lon"}


def _v(name: str, length: int = 40, *, not_null: bool = False) -> Column:
    return Column(name, "VARCHAR", length=length, not_null=not_null)


TABLES: tuple[Table, ...] = (
    Table(
        "business_associates",
        "Internal and external business associates (legal entities) known to the ETRM.",
        (
            _v("ba_code", 20, not_null=True),
            _v("name", 120, not_null=True),
            _v("org_id", 40),
            _v("ba_type", 10, not_null=True),
        ),
        ("ba_code",),
    ),
    Table(
        "products",
        "ETRM product codes.",
        (_v("product_code", 20, not_null=True), _v("name", 60, not_null=True)),
        ("product_code",),
    ),
    Table(
        "deal_header",
        "One row per deal version: parties, side, status and the reference to the email confirmation.",
        (
            _v("deal_number", 20, not_null=True),
            Column("version", "INT", not_null=True),
            Column("deal_date", "DATE", not_null=True),
            _v("trader", 20),
            _v("internal_ba", 20, not_null=True),
            _v("external_ba", 20, not_null=True),
            _v("buy_sell", 1, not_null=True),
            _v("deal_status", 20, not_null=True),
            _v("external_ref", 30),
            _v("change_reason", 200),
        ),
        ("deal_number", "version"),
        (("internal_ba", "business_associates", "ba_code"), ("external_ba", "business_associates", "ba_code")),
    ),
    Table(
        "deal_detail",
        "Commodity, delivery, quantity and price of each deal version.",
        (
            _v("deal_number", 20, not_null=True),
            Column("version", "INT", not_null=True),
            _v("product_code", 20, not_null=True),
            _v("location", 60),
            _v("delivery_term", 10),
            _v("period", 10),
            Column("quantity", "NUMERIC", precision=20, scale=3),
            _v("uom", 5),
            _v("price_type", 10),
            Column("fixed_price", "NUMERIC", precision=20, scale=3),
            _v("price_index", 60),
            Column("premium", "NUMERIC", precision=20, scale=3),
            _v("currency", 3),
        ),
        ("deal_number", "version"),
        (("product_code", "products", "product_code"),),
    ),
)
TABLES_BY_NAME = {t.name: t for t in TABLES}

# deal_detail references its header on the composite key; written as a table-level constraint in the DDL.
_DETAIL_HEADER_FK = "FOREIGN KEY (deal_number, version) REFERENCES rightangle.deal_header (deal_number, version)"


def _none(value: Any) -> Any:
    return None if value == "" else value


def etrm_rows(result: BuildResult) -> dict[str, list[dict[str, Any]]]:
    """Rows per ETRM table, from every execution scenario's booked deal versions."""
    rows: dict[str, list[dict[str, Any]]] = {t.name: [] for t in TABLES}
    orgs = result.universe["orgs"]
    bas: dict[str, dict[str, Any]] = {}
    products: dict[str, str] = {}
    for sc in result.scenarios:
        if sc.phase != "execution":
            continue
        ex = sc.execution
        for code, org in INTERNAL_BA.items():
            bas[code] = {"ba_code": code, "name": orgs[org]["name"], "org_id": org, "ba_type": "INTERNAL"}
        for dl in ex["dialects"]:
            bas[dl["etrm_ba"]] = {
                "ba_code": dl["etrm_ba"],
                "name": orgs[dl["counterparty"]]["name"],
                "org_id": dl["counterparty"],
                "ba_type": "EXTERNAL",
            }
        products.update(ex["product_codes"])
        for r in ex["etrm_rows"]:
            rows["deal_header"].append(
                {
                    "deal_number": r["DealNumber"],
                    "version": r["Version"],
                    "deal_date": r["DealDate"],
                    "trader": r["Trader"],
                    "internal_ba": r["InternalBA"],
                    "external_ba": r["ExternalBA"],
                    "buy_sell": r["BuySell"],
                    "deal_status": r["DealStatus"],
                    "external_ref": r["ExternalRef"],
                    "change_reason": _none(r["ChangeReason"]),
                }
            )
            rows["deal_detail"].append(
                {
                    "deal_number": r["DealNumber"],
                    "version": r["Version"],
                    "product_code": r["ProductCode"],
                    "location": r["Location"],
                    "delivery_term": r["DeliveryTerm"],
                    "period": r["Period"],
                    "quantity": r["Quantity"],
                    "uom": r["UOM"],
                    "price_type": r["PriceType"],
                    "fixed_price": _none(r["FixedPrice"]),
                    "price_index": _none(r["PriceIndex"]),
                    "premium": _none(r["Premium"]),
                    "currency": r["Currency"],
                }
            )
    rows["business_associates"] = [bas[k] for k in sorted(bas)]
    rows["products"] = [{"product_code": k, "name": products[k]} for k in sorted(products)]
    for name in ("deal_header", "deal_detail"):
        rows[name].sort(key=lambda r: (r["deal_number"], r["version"]))
    return rows


def render_schema() -> bytes:
    lines = [
        "-- RightAngle-style ETRM deal capture (separate datasource). Generated; do not edit.",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};",
        "",
    ]
    for t in TABLES:
        lines += [f"-- {t.description}", f"CREATE TABLE {SCHEMA_NAME}.{t.name} ("]
        parts = [f"    {c.name} {c.sql_type()}{' NOT NULL' if c.not_null else ''}" for c in t.columns]
        parts.append(f"    PRIMARY KEY ({', '.join(t.primary_key)})")
        parts += [f"    FOREIGN KEY ({col}) REFERENCES {SCHEMA_NAME}.{ref} ({rc})" for col, ref, rc in t.foreign_keys]
        if t.name == "deal_detail":
            parts.append(f"    {_DETAIL_HEADER_FK}")
        lines += [",\n".join(parts), ");", ""]
    return "\n".join(lines).encode("utf-8")


def render_seed(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> bytes:
    lines = ["-- RightAngle-style ETRM deal capture: seed data, from the same rows as the CSV files.", ""]
    for t in TABLES:
        names = ", ".join(c.name for c in t.columns)
        for row in rows[t.name]:
            values = ", ".join(_sql_literal(c, row[c.name]) for c in t.columns)
            lines.append(f"INSERT INTO {SCHEMA_NAME}.{t.name} ({names}) VALUES ({values});")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def catalog_fixtures() -> dict[str, Any]:
    """The same fixture shapes as ``structured.catalog_fixtures``, for the ETRM datasource."""
    database = {
        "id": DB_ID,
        "name": DATABASE_NAME,
        "displayName": "RightAngle ETRM",
        "fullyQualifiedName": DB_FQN,
        "description": "",
        "service": {
            "id": SERVICE_ID,
            "type": "databaseService",
            "name": "etrm_sqlserver",
            "fullyQualifiedName": "etrm_sqlserver",
            "serviceType": "Mssql",
        },
        "tags": [],
        "owners": [],
    }
    schema = {
        "id": SCHEMA_ID,
        "name": SCHEMA_NAME,
        "displayName": "RightAngle deal capture",
        "fullyQualifiedName": SCHEMA_FQN,
        "description": "",
        "database": {"id": DB_ID, "type": "database", "name": DATABASE_NAME, "fullyQualifiedName": DB_FQN},
    }

    def fks(t: Table) -> list[tuple[list[str], str, list[str]]]:
        out = [([col], ref, [rc]) for col, ref, rc in t.foreign_keys]
        if t.name == "deal_detail":
            out.append((["deal_number", "version"], "deal_header", ["deal_number", "version"]))
        return out

    tables = []
    source_tables = []
    for i, t in enumerate(TABLES, 1):
        columns = []
        for n, c in enumerate(t.columns, 1):
            col: dict[str, Any] = {"name": c.name, "dataType": c.type, "ordinalPosition": n}
            if c.length is not None:
                col["dataLength"] = c.length
            if c.precision is not None:
                col["precision"], col["scale"] = c.precision, c.scale
            if c.not_null:
                col["constraint"] = "NOT_NULL"
            columns.append(col)
        constraints: list[dict[str, Any]] = [{"constraintType": "PRIMARY_KEY", "columns": list(t.primary_key)}]
        constraints += [
            {
                "constraintType": "FOREIGN_KEY",
                "columns": cols,
                "referredColumns": [f"{SCHEMA_FQN}.{ref}.{rc}" for rc in rcs],
            }
            for cols, ref, rcs in fks(t)
        ]
        tables.append(
            {
                "id": f"etrm-{i:03d}",
                "name": t.name,
                "fullyQualifiedName": f"{SCHEMA_FQN}.{t.name}",
                "tableType": "Regular",
                "databaseSchema": {
                    "id": SCHEMA_ID,
                    "type": "databaseSchema",
                    "name": SCHEMA_NAME,
                    "fullyQualifiedName": SCHEMA_FQN,
                },
                "columns": columns,
                "tableConstraints": constraints,
                "tags": [],
            }
        )
        source_tables.append(
            {
                "name": t.name,
                "technicalMetadata": {"columnCount": len(t.columns), "format": "mssql"},
                "businessMetadata": {
                    "description": t.description,
                    "enrichmentSource": "DETERMINISTIC",
                    "reviewStatus": "APPROVED",
                },
                "primaryKey": {"columns": list(t.primary_key), "source": "DETERMINISTIC", "confidence": 1.0},
                "foreignKeys": [
                    {
                        "column": ", ".join(cols),
                        "targetTable": ref,
                        "targetColumn": ", ".join(rcs),
                        "source": "DETERMINISTIC",
                        "confidence": 1.0,
                    }
                    for cols, ref, rcs in fks(t)
                ],
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
    return {"databases": [database], "databaseSchemas": [schema], "tables": tables, "sample_data": sample}


def render_etrm(result: BuildResult) -> dict[str, bytes]:
    """Every ETRM file, keyed by path relative to ``corpus/``; empty when no execution scenario was built."""
    rows = etrm_rows(result)
    if not rows["deal_header"]:
        return {}
    files = {f"{ETRM_DIR}/{t.name}.csv": render_csv(t, rows[t.name]) for t in TABLES}
    files[f"{ETRM_DIR}/schema.sql"] = render_schema()
    files[f"{ETRM_DIR}/seed.sql"] = render_seed(rows)
    fx = catalog_fixtures()
    for name in ("databases", "databaseSchemas", "tables"):
        files[f"{CATALOG_DIR}/{name}.json"] = _json(fx[name])
    files[f"{CATALOG_DIR}/sample_data/rightangle.json"] = _json(fx["sample_data"])
    return files
