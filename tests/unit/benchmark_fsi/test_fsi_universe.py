# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The NewCo universe: determinism, independence of entities, ownership arithmetic, ontology guard."""

from __future__ import annotations

from datetime import date

import pytest

from tests.benchmark.fsi_commodity import ontology
from tests.benchmark.fsi_commodity.calendars import CALENDARS, add_business_days, is_business_day
from tests.benchmark.fsi_commodity.universe import (
    DEFAULT_SEED,
    ROLES,
    beneficial_owners,
    build_universe,
    dumps,
    ownership_paths,
)
from tests.unit.benchmark_fsi.test_fsi_ids import valid_aba, valid_abn, valid_iban, valid_lei

pytestmark = pytest.mark.unit

NEWCO = ["newco-syd", "newco-hou", "newco-nyc", "newco-lon"]


@pytest.fixture(scope="module")
def uni() -> dict:
    return build_universe()


def test_same_seed_gives_identical_json_bytes_and_other_seed_differs() -> None:
    assert dumps(build_universe(DEFAULT_SEED)) == dumps(build_universe(DEFAULT_SEED))
    assert dumps(build_universe(DEFAULT_SEED)) != dumps(build_universe(DEFAULT_SEED + 1))


def test_adding_a_counterparty_leaves_other_entities_unchanged(uni: dict) -> None:
    extra = {
        "id": "cp-lng-test",
        "name": "Marlin LNG Trading Pte Ltd",
        "legal_form": "Pte Ltd",
        "jurisdiction": "Singapore",
        "country": "GB",
        "city": "London",
        "calendar": "GB",
        "domain": "marlinlng.example",
        "accounts": (("USD", "bank-northgate", "operating"),),
    }
    bigger = build_universe(DEFAULT_SEED, extra_counterparties=[extra])
    assert "cp-lng-test" in bigger["orgs"]
    for section in ("orgs", "persons", "accounts"):
        for key, value in uni[section].items():
            assert bigger[section][key] == value, (section, key)


def test_group_structure_and_mailboxes(uni: dict) -> None:
    assert uni["orgs"]["newco-hldg"]["name"] == "NewCo Global Holdings, Inc."
    forms = {oid: uni["orgs"][oid]["legal_form"] for oid in NEWCO}
    assert forms == {"newco-syd": "Pty Ltd", "newco-hou": "LLC", "newco-nyc": "Inc.", "newco-lon": "Limited"}
    for oid in NEWCO:
        org = uni["orgs"][oid]
        assert [uni["persons"][p]["role"] for p in org["people"]] == list(ROLES)
        assert all(uni["persons"][p]["email"].endswith(f"@{org['domain']}") for p in org["people"])
        assert org["domain"].startswith("newco-")
        assert org["accounts"]


def test_every_mailbox_is_an_example_domain_and_resolves(uni: dict) -> None:
    assert uni["directory"]
    for address, entry in uni["directory"].items():
        assert address.endswith(".example")
        assert uni["persons"][entry["person"]]["email"] == address
        assert entry["org"] in uni["orgs"]


def test_founders_are_beneficial_owners_of_every_newco_entity(uni: dict) -> None:
    for oid in ["newco-hldg", *NEWCO]:
        assert beneficial_owners(uni, oid) == {"own-founder-1": 34.1, "own-founder-2": 27.9}, oid


def test_trust_layers_resolve_at_two_to_four_hops(uni: dict) -> None:
    assert beneficial_owners(uni, "cp-kestrel") == {"own-kestrel-founder": 45.0, "own-kestrel-pep": 55.0}
    assert beneficial_owners(uni, "cp-tiberpeak") == {"own-tiber-declared": 40.0, "own-tiber-hidden": 60.0}
    (kestrel_path,) = ownership_paths(uni, "own-kestrel-pep", "cp-kestrel")
    (tiber_path,) = ownership_paths(uni, "own-tiber-hidden", "cp-tiberpeak")
    assert len(kestrel_path) - 1 == 3
    assert len(tiber_path) - 1 == 4
    assert "hold-tiber-cy" in tiber_path


def test_consolidation_classes_come_from_the_pack(uni: dict) -> None:
    for edge in uni["ownership_edges"]:
        if edge["class_uri"] is not None:
            assert ontology.is_allowed(edge["class_uri"])
            assert edge["class_uri"].rsplit("/", 1)[-1] == edge["relationship"]
    assert any(e["relationship"] == "UltimateConsolidation" for e in uni["ownership_edges"])
    assert any(e["relationship"] == "DirectConsolidation" for e in uni["ownership_edges"])


def test_identifiers_are_checksum_valid(uni: dict) -> None:
    for org in uni["orgs"].values():
        assert org["lei"].startswith("SYNT")
        assert valid_lei(org["lei"])
        if org["tax_id"] and org["tax_id"].startswith("ABN "):
            assert valid_abn(org["tax_id"][4:])
        if "aba" in org:
            assert valid_aba(org["aba"])
    for acct in uni["accounts"].values():
        if acct["iban"]:
            assert valid_iban(acct["iban"])
        if acct["aba"]:
            assert valid_aba(acct["aba"])
        assert acct["bic"][4:6] == uni["orgs"][acct["bank"]]["country"]


def test_sanctions_and_pep_flags_only_on_fictional_parties(uni: dict) -> None:
    flagged = {pid for pid, p in uni["persons"].items() if p["pep"] or p["sanctioned"]}
    assert flagged == {"own-kestrel-pep", "own-tiber-hidden"}
    for oid in ("cp-vitol", "cp-glencore", "cp-shell", "cp-bp", "cp-bp-au"):
        assert uni["orgs"][oid]["real_name"]
        assert not set(beneficial_owners(uni, oid, 0)) & flagged


def test_counterparties_and_service_parties_present(uni: dict) -> None:
    for oid in ("cp-vitol", "cp-glencore", "cp-shell", "cp-bp", "cp-harbourline", "cp-kestrel", "cp-tiberpeak"):
        assert oid in uni["orgs"]
    assert "Glencore Limited" in uni["orgs"]["cp-glencore"]["aliases"]
    names = {o["name"] for o in uni["orgs"].values()}
    assert {"Cardinal Inspection Services", "Gulfstream Marine Agency", "Northgate Bank plc"} <= names
    assert sum(o["kind"] == "law_firm" for o in uni["orgs"].values()) == 2


def test_ontology_lookup_rejects_invented_classes() -> None:
    assert ontology.cls("Invoice").endswith("/Invoice")
    with pytest.raises(KeyError):
        ontology.cls("TotallyMadeUpClass")
    with pytest.raises(KeyError):
        ontology.cls("LegalEntity")  # ambiguous: GLEIF and FIBO BE share the name
    assert "gleif" in ontology.cls("LegalEntity", "gleif").lower()
    assert not ontology.is_allowed("https://example.com/Invented")


def test_business_day_arithmetic_differs_by_jurisdiction() -> None:
    # 2026-01-26 is Australia Day (NSW holiday) but an ordinary Monday in the US and UK.
    monday = date(2026, 1, 26)
    assert not is_business_day(CALENDARS["AU-NSW"], monday)
    assert is_business_day(CALENDARS["US-TX"], monday)
    assert add_business_days(CALENDARS["AU-NSW"], date(2026, 1, 23), 1) == date(2026, 1, 27)
    assert add_business_days(CALENDARS["US-TX"], date(2026, 1, 23), 1) == monday
    with pytest.raises(ValueError):
        is_business_day(CALENDARS["GB"], date(2031, 1, 6))
