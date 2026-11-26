# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The NewCo world: legal entities, people, accounts, ownership and mailboxes.

``build_universe`` derives every entity from ``seed + crc32(key)``, so adding an entity leaves the
others unchanged. Every person and fact is invented. Real counterparty names (Vitol, Glencore, Shell,
BP) are used only so entity resolution has real names to match; sanctions and PEP findings attach
only to people behind the fictional counterparties.
"""

# ruff: noqa: E501
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from faker import Faker

from . import ids
from .ids import DEFAULT_SEED
from .ontology import cls

__all__ = [
    "DEFAULT_SEED",
    "ROLES",
    "beneficial_owners",
    "build_universe",
    "dumps",
    "ownership_paths",
    "write_universe",
]

ROLES = ("marketer", "trader", "lawyer", "credit_analyst", "compliance_analyst", "ops", "settlements", "accounting")

_LOCALES = {"AU": "en_AU", "US": "en_US", "GB": "en_GB", "AE": "en_US", "CY": "en_GB", "VG": "en_GB", "JE": "en_GB"}
_FAKERS: dict[str, Faker] = {}


def _faker(locale: str, seed: int, key: str) -> Faker:
    """Return a Faker for ``locale`` re-seeded to ``seed + crc32(key)`` (deterministic per entity)."""
    fake = _FAKERS.get(locale)
    if fake is None:
        fake = _FAKERS[locale] = Faker(locale)
    fake.seed_instance(ids.key_seed(seed, key))
    return fake


def _slug(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


# --- specifications -------------------------------------------------------------------------------

_NEWCO = (
    # id, name, legal form, inc. jurisdiction, country, state, city, postcode, calendar, domain, accounts
    ("newco-hldg", "NewCo Global Holdings, Inc.", "Inc.", "Delaware", "US", "NY", "New York", "10005", "US-NY", "newco-global.example", ()),
    ("newco-syd", "NewCo Trading Sydney Pty Ltd", "Pty Ltd", "New South Wales", "AU", "NSW", "Sydney", "2000", "AU-NSW", "newco-sydney.example", (("AUD", "bank-bluegum", "operating"), ("USD", "bank-bluegum", "operating"))),
    ("newco-hou", "NewCo Trading Houston LLC", "LLC", "Texas", "US", "TX", "Houston", "77002", "US-TX", "newco-houston.example", (("USD", "bank-lonestar", "operating"), ("USD", "bank-lonestar", "settlement"))),
    ("newco-nyc", "NewCo Derivatives New York, Inc.", "Inc.", "Delaware", "US", "NY", "New York", "10005", "US-NY", "newco-newyork.example", (("USD", "bank-hudson", "operating"), ("USD", "bank-hudson", "collateral"))),
    ("newco-lon", "NewCo Trading London Limited", "Limited", "England and Wales", "GB", "", "London", "EC2V 7HR", "GB", "newco-london.example", (("GBP", "bank-northgate", "operating"), ("USD", "bank-northgate", "operating"))),
)  # fmt: skip

# id, name, legal form, inc. jurisdiction, country, state, city, postcode, calendar, domain, real_name, accounts
_COUNTERPARTIES = (
    ("cp-vitol", "Vitol Inc.", "Inc.", "Delaware", "US", "TX", "Houston", "77056", "US-TX", "vitol.example", True, (("USD", "bank-hudson", "operating"),)),
    ("cp-glencore", "Glencore Ltd.", "Ltd.", "Delaware", "US", "CT", "Stamford", "06902", "US-NY", "glencore.example", True, (("USD", "bank-hudson", "operating"),)),
    ("cp-shell", "Shell Trading International Ltd", "Ltd", "England and Wales", "GB", "", "London", "SE1 7NA", "GB", "shell.example", True, (("USD", "bank-northgate", "operating"),)),
    ("cp-bp", "BP Oil International Limited", "Limited", "England and Wales", "GB", "", "London", "EC2Y 9LY", "GB", "bp.example", True, (("USD", "bank-northgate", "operating"),)),
    ("cp-bp-au", "BP Australia Pty Ltd", "Pty Ltd", "Victoria", "AU", "VIC", "Melbourne", "3000", "AU-NSW", "bp-australia.example", True, (("AUD", "bank-southerntide", "operating"), ("USD", "bank-southerntide", "operating"))),
    ("cp-harbourline", "Harbourline Petroleum Pty Ltd", "Pty Ltd", "New South Wales", "AU", "NSW", "Newcastle", "2300", "AU-NSW", "harbourline.example", False, (("AUD", "bank-southerntide", "operating"), ("USD", "bank-southerntide", "operating"))),
    ("cp-kestrel", "Kestrel Bunkering Limited", "Limited", "England and Wales", "GB", "", "London", "E14 5AB", "GB", "kestrel.example", False, (("USD", "bank-northgate", "operating"),)),
    ("cp-tiberpeak", "Tiber Peak Trading DMCC", "DMCC", "Dubai", "AE", "", "Dubai", "00000", "AE", "tiberpeak.example", False, (("USD", "bank-gulfcrescent", "operating"),)),
)  # fmt: skip

# Ownership-chain entities and holding companies: id, name, legal form, inc. jurisdiction, country, kind
_HOLDERS = (
    ("hold-founders", "NewCo Founders Partnership", "General Partnership", "Delaware", "US", "holder"),
    ("hold-infra", "Ridgeline Infrastructure Fund II LP", "LP", "Delaware", "US", "holder"),
    ("hold-mgmt", "NewCo Management Pool LLC", "LLC", "Delaware", "US", "holder"),
    ("hold-kestrel-bvi", "Kestrel Holdings (BVI) Ltd", "Ltd", "British Virgin Islands", "VG", "holder"),
    ("hold-kestrel-trust", "Sandpiper Trust (Jersey) Limited", "Limited", "Jersey", "JE", "holder"),
    ("hold-tiber-cy", "Tiber Peak Holdings Ltd", "Ltd", "Cyprus", "CY", "holder"),
    ("hold-tiber-nominee", "Aurelian Nominees Ltd", "Ltd", "Cyprus", "CY", "holder"),
    ("hold-tiber-foundation", "Corvane Foundation", "Foundation", "Panama", "PA", "holder"),
    ("hold-harbourline-grp", "Harbourline Group Holdings Pty Ltd", "Pty Ltd", "New South Wales", "AU", "holder"),
)  # fmt: skip

# Service parties: id, name, kind, country, city, calendar, domain, roles
_BANKS = (
    ("bank-northgate", "Northgate Bank plc", "bank", "GB", "London", "GB", "northgatebank.example", ("trade_finance", "operations")),
    ("bank-bluegum", "Bluegum Commercial Bank Ltd", "bank", "AU", "Sydney", "AU-NSW", "bluegumbank.example", ("trade_finance", "operations")),
    ("bank-lonestar", "Lonestar National Bank", "bank", "US", "Houston", "US-TX", "lonestarbank.example", ("trade_finance", "operations")),
    ("bank-hudson", "Hudson Meridian Trust Company", "bank", "US", "New York", "US-NY", "hudsonmeridian.example", ("trade_finance", "operations")),
    ("bank-southerntide", "Southern Tide Bank Ltd", "bank", "AU", "Newcastle", "AU-NSW", "southerntidebank.example", ("trade_finance", "operations")),
    ("bank-concord", "Concord Reserve Bank plc", "bank", "GB", "London", "GB", "concordreserve.example", ("trade_finance", "operations")),
    ("bank-gulfcrescent", "Gulf Crescent Bank PJSC", "bank", "AE", "Dubai", "AE", "gulfcrescent.example", ("trade_finance", "operations")),
)  # fmt: skip

_SERVICES = (
    ("svc-calloway", "Calloway Rye LLP", "law_firm", "GB", "London", "GB", "callowayrye.example", ("partner", "associate")),
    ("svc-merriweather", "Merriweather Ng Lawyers", "law_firm", "AU", "Sydney", "AU-NSW", "merriweatherng.example", ("partner", "associate")),
    ("svc-veridian", "Veridian Screening Ltd", "screening_vendor", "GB", "London", "GB", "veridianscreening.example", ("analyst", "support")),
    ("svc-ledgerline", "Ledgerline Credit Bureau", "credit_bureau", "US", "New York", "US-NY", "ledgerline.example", ("analyst", "support")),
    ("svc-cardinal", "Cardinal Inspection Services", "inspector", "US", "Houston", "US-TX", "cardinalinspection.example", ("inspector", "reports")),
    ("svc-gulfstream", "Gulfstream Marine Agency", "shipping_agent", "US", "Houston", "US-TX", "gulfstreammarine.example", ("agent", "operations")),
)  # fmt: skip

_STREETS = {"US": "Main", "GB": "High", "AU": "George", "AE": "Sheikh Zayed"}

# Natural persons behind ownership chains: id, org (the legal entity they own), locale country, pep, sanctioned
_OWNERS = (
    ("own-founder-1", "NewCo founder (managing partner)", "US", False, False),
    ("own-founder-2", "NewCo founder (partner)", "US", False, False),
    ("own-kestrel-founder", "Kestrel founder", "GB", False, False),
    ("own-kestrel-pep", "Kestrel trust beneficiary (politically exposed)", "GB", True, False),
    ("own-harbourline-1", "Harbourline principal", "AU", False, False),
    ("own-harbourline-2", "Harbourline principal", "AU", False, False),
    ("own-tiber-declared", "Tiber Peak declared shareholder", "AE", False, False),
    ("own-tiber-hidden", "Tiber Peak concealed owner (sanctioned)", "AE", False, True),
)

# parent, child, percent
_EDGES = (
    ("own-founder-1", "hold-founders", 55.0),
    ("own-founder-2", "hold-founders", 45.0),
    ("hold-founders", "newco-hldg", 62.0),
    ("hold-infra", "newco-hldg", 28.0),
    ("hold-mgmt", "newco-hldg", 10.0),
    ("newco-hldg", "newco-syd", 100.0),
    ("newco-hldg", "newco-hou", 100.0),
    ("newco-hldg", "newco-nyc", 100.0),
    ("newco-hldg", "newco-lon", 100.0),
    ("cp-bp", "cp-bp-au", 100.0),
    ("own-harbourline-1", "hold-harbourline-grp", 70.0),
    ("own-harbourline-2", "hold-harbourline-grp", 30.0),
    ("hold-harbourline-grp", "cp-harbourline", 100.0),
    ("own-kestrel-pep", "hold-kestrel-trust", 100.0),
    ("hold-kestrel-trust", "hold-kestrel-bvi", 55.0),
    ("own-kestrel-founder", "hold-kestrel-bvi", 45.0),
    ("hold-kestrel-bvi", "cp-kestrel", 100.0),
    ("own-tiber-hidden", "hold-tiber-foundation", 100.0),
    ("hold-tiber-foundation", "hold-tiber-nominee", 100.0),
    ("hold-tiber-nominee", "hold-tiber-cy", 60.0),
    ("own-tiber-declared", "hold-tiber-cy", 40.0),
    ("hold-tiber-cy", "cp-tiberpeak", 100.0),
)

_REG_PREFIX = {"US": "", "GB": "", "AU": "ACN ", "AE": "DMCC-", "CY": "HE", "VG": "BVI-", "JE": "JE-", "PA": "PA-"}


# --- builders -------------------------------------------------------------------------------------


def _incorporation(seed: int, key: str, jurisdiction: str, country: str) -> dict[str, str]:
    r = ids.rng_for(seed, "inc:" + key)
    founded = date(2003, 1, 1) + timedelta(days=r.randint(0, 365 * 14))
    number = f"{_REG_PREFIX.get(country, '')}{r.randint(1000000, 99999999)}"
    return {"jurisdiction": jurisdiction, "date": founded.isoformat(), "registration_number": number}


def _tax_id(seed: int, key: str, country: str) -> str | None:
    if country == "AU":
        return "ABN " + ids.abn(seed, key)
    if country == "US":
        return "EIN " + ids.ein(seed, key)
    return None


def _staff(
    seed: int, org_id: str, country: str, domain: str, roles: Sequence[str], persons: dict, directory: dict
) -> list[str]:
    """Create one person per role, each seeded from ``seed + crc32('person:<org>:<role>')``."""
    created: list[str] = []
    used: set[str] = set()
    for role in roles:
        key = f"person:{org_id}:{role}"
        fake = _faker(_LOCALES[country], seed, key)
        first, last = fake.first_name(), fake.last_name()
        local = f"{_slug(first)}.{_slug(last)}"
        while local in used:
            local += "x"
        used.add(local)
        address = f"{local}@{domain}"
        person_id = f"p-{org_id}-{role}"
        persons[person_id] = {
            "id": person_id,
            "name": f"{first} {last}",
            "org": org_id,
            "role": role,
            "email": address,
            "phone": fake.phone_number(),
            "kind": "staff",
            "pep": False,
            "sanctioned": False,
        }
        directory[address] = {"org": org_id, "role": role, "person": person_id}
        created.append(person_id)
    return created


def _org(seed: int, o: dict[str, Any], persons: dict, directory: dict) -> dict[str, Any]:
    key = o["id"]
    fake = _faker(_LOCALES.get(o["country"], "en_GB"), seed, "org:" + key)
    street = f"{fake.building_number()} {_STREETS.get(o['country'], 'High')} Street"
    record: dict[str, Any] = {
        "id": key,
        "name": o["name"],
        "legal_form": o["legal_form"],
        "kind": o["kind"],
        "country": o["country"],
        "calendar": o.get("calendar"),
        "domain": o.get("domain"),
        "real_name": o.get("real_name", False),
        "aliases": list(o.get("aliases", ())),
        "lei": ids.lei(seed, key),
        "incorporation": _incorporation(seed, key, o["jurisdiction"], o["country"]),
        "tax_id": _tax_id(seed, key, o["country"]),
        "address": {
            "street": street if o.get("city") else None,
            "city": o.get("city"),
            "state": o.get("state", ""),
            "postcode": o.get("postcode", ""),
            "country": o["country"],
        },
        "accounts": [],
        "people": [],
        "class_uri": cls("LegalEntity", "gleif"),
    }
    if o.get("domain"):
        record["people"] = _staff(seed, key, o["country"], o["domain"], o["roles"], persons, directory)
    return record


def _bank(seed: int, o: dict[str, Any]) -> dict[str, Any]:
    key = o["id"]
    extra: dict[str, Any] = {"bic": ids.bic(seed, key, o["country"])}
    if o["country"] == "US":
        extra["aba"] = ids.aba(seed, key)
    if o["country"] == "AU":
        extra["bsb"] = ids.bsb(seed, key)
    return extra


def _account(seed: int, owner: dict[str, Any], bank: dict[str, Any], currency: str, purpose: str) -> dict[str, Any]:
    acct_id = f"acct-{owner['id']}-{currency.lower()}-{purpose}"
    number = ids.account_number(seed, acct_id, 8 if bank["country"] == "GB" else 10)
    record: dict[str, Any] = {
        "id": acct_id,
        "owner": owner["id"],
        "bank": bank["id"],
        "currency": currency,
        "purpose": purpose,
        "name": owner["name"],
        "number": number,
        "bic": bank["bic"],
        "iban": None,
        "aba": bank.get("aba"),
        "bsb": bank.get("bsb"),
        "class_uri": cls("Account", "fibo"),
    }
    if bank["country"] in ("GB", "AE"):
        record["iban"] = ids.iban(seed, acct_id, bank["country"])
        if bank["country"] == "GB":  # a GB IBAN's last 8 digits are the account number
            record["number"] = record["iban"][-8:]
    return record


def build_universe(seed: int = DEFAULT_SEED, extra_counterparties: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Build the whole world as a JSON-ready dict.

    ``extra_counterparties`` adds counterparties (same keys as the built-in specs: ``id``, ``name``,
    ``legal_form``, ``jurisdiction``, ``country``, ``state``, ``city``, ``postcode``, ``calendar``,
    ``domain``, ``accounts``). Each is derived from ``seed + crc32(id)``, so existing entities do not change.
    """
    persons: dict[str, Any] = {}
    directory: dict[str, Any] = {}
    orgs: dict[str, Any] = {}
    accounts: dict[str, Any] = {}

    specs: list[dict[str, Any]] = []
    for oid, name, form, jur, country, state, city, post, cal, domain, accts in _NEWCO:
        specs.append(dict(id=oid, name=name, legal_form=form, jurisdiction=jur, country=country, state=state, city=city,
                          postcode=post, calendar=cal, domain=domain, kind="newco", roles=ROLES, accounts=accts))  # fmt: skip
    cp_rows: list[dict[str, Any]] = []
    for oid, name, form, jur, country, state, city, post, cal, domain, real, cp_accts in _COUNTERPARTIES:
        cp_rows.append(dict(id=oid, name=name, legal_form=form, jurisdiction=jur, country=country, state=state, city=city,
                            postcode=post, calendar=cal, domain=domain, real_name=real, accounts=cp_accts))  # fmt: skip
    cp_rows.extend(dict(c) for c in extra_counterparties)
    for c in cp_rows:
        specs.append({**c, "kind": "counterparty", "roles": ROLES, "accounts": tuple(c.get("accounts", ()))})
        if c["id"] == "cp-glencore":
            specs[-1]["aliases"] = ("Glencore Limited", "Glencore Ltd")
    for oid, name, form, jur, country, kind in _HOLDERS:
        specs.append(
            dict(id=oid, name=name, legal_form=form, jurisdiction=jur, country=country, kind=kind, accounts=())
        )
    for oid, name, kind, country, city, cal, domain, roles in (*_BANKS, *_SERVICES):
        specs.append(dict(id=oid, name=name, legal_form="", jurisdiction=country, country=country, city=city, calendar=cal,
                          domain=domain, kind=kind, roles=roles, accounts=()))  # fmt: skip

    for spec in specs:
        record = _org(seed, spec, persons, directory)
        if spec["kind"] == "bank":
            record.update(_bank(seed, spec))
        orgs[record["id"]] = record
    for spec in specs:
        owner = orgs[spec["id"]]
        for currency, bank_id, purpose in spec["accounts"]:
            acct = _account(seed, owner, orgs[bank_id], currency, purpose)
            accounts[acct["id"]] = acct
            owner["accounts"].append(acct["id"])

    for oid, label, country, pep, sanctioned in _OWNERS:
        fake = _faker(_LOCALES[country], seed, "person:" + oid)
        persons[oid] = {
            "id": oid,
            "name": f"{fake.first_name()} {fake.last_name()}",
            "org": None,
            "role": label,
            "email": None,
            "phone": None,
            "kind": "owner",
            "pep": pep,
            "sanctioned": sanctioned,
        }

    edges = _edges(orgs)
    universe = {
        "seed": seed,
        "ownership_edges": edges,
        "orgs": orgs,
        "persons": persons,
        "accounts": accounts,
        "directory": directory,
    }
    _assert_findings_fictional(universe)
    return universe


def _edges(orgs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build ownership edges. Consolidation classes apply to legal-entity parents holding over 50%."""
    has_org_parent = {child for parent, child, _ in _EDGES if parent in orgs}
    edges = []
    for parent, child, pct in _EDGES:
        relationship = None
        if parent in orgs and pct > 50:
            relationship = "UltimateConsolidation" if parent not in has_org_parent else "DirectConsolidation"
        edges.append(
            {
                "parent": parent,
                "child": child,
                "percent": pct,
                "relationship": relationship,
                "class_uri": cls(relationship, "gleif") if relationship else None,
            }
        )
    return edges


def _assert_findings_fictional(universe: Mapping[str, Any]) -> None:
    """Fail the build if a sanctions or PEP flag sits on anyone tied to a real-name counterparty."""
    real = {oid for oid, o in universe["orgs"].items() if o["real_name"]}
    for pid, p in universe["persons"].items():
        if (p["pep"] or p["sanctioned"]) and (p["org"] in real or _owns_real(universe, pid, real)):
            raise ValueError(f"{pid} carries a sanctions/PEP flag but is tied to a real-name party")


def _owns_real(universe: Mapping[str, Any], person_id: str, real: set[str]) -> bool:
    edges = universe["ownership_edges"]
    frontier, seen = [person_id], set()
    while frontier:
        node = frontier.pop()
        for e in edges:
            if e["parent"] == node and e["child"] not in seen:
                if e["child"] in real:
                    return True
                seen.add(e["child"])
                frontier.append(e["child"])
    return False


# --- ownership queries ----------------------------------------------------------------------------


def ownership_paths(universe: Mapping[str, Any], parent_id: str, entity_id: str) -> list[list[str]]:
    """Return every ownership path (node ids, parent first) from ``parent_id`` down to ``entity_id``."""
    edges = universe["ownership_edges"]
    paths: list[list[str]] = []

    def walk(node: str, trail: list[str]) -> None:
        if node == entity_id:
            paths.append(trail)
            return
        for e in edges:
            if e["parent"] == node:
                walk(e["child"], [*trail, e["child"]])

    walk(parent_id, [parent_id])
    return paths


def beneficial_owners(universe: Mapping[str, Any], entity_id: str, threshold: float = 25.0) -> dict[str, float]:
    """Return ``{person_id: effective percent}`` for natural persons holding at least ``threshold`` percent."""
    edges = universe["ownership_edges"]
    persons = universe["persons"]
    totals: dict[str, Decimal] = {}

    def up(node: str, share: Decimal) -> None:
        for e in edges:
            if e["child"] == node:
                portion = share * Decimal(str(e["percent"])) / 100
                if e["parent"] in persons:
                    totals[e["parent"]] = totals.get(e["parent"], Decimal(0)) + portion
                else:
                    up(e["parent"], portion)

    up(entity_id, Decimal(100))
    return {pid: float(pct) for pid, pct in sorted(totals.items()) if pct >= Decimal(str(threshold))}


# --- output ---------------------------------------------------------------------------------------


def dumps(universe: Mapping[str, Any]) -> bytes:
    """Serialise with sorted keys, 2-space indent, ``\\n`` newlines and a trailing newline."""
    return (json.dumps(universe, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_universe(universe: Mapping[str, Any], path: Path) -> None:
    """Write ``universe.json`` (binary mode, so line endings never change)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(universe))
