# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Class URIs the corpus may ground to: only those listed in the pack's ``sample_classes``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PACK_PATH = Path(__file__).resolve().parents[3] / "packages" / "ontology-engine" / "demo" / "config-fsi-commodity.yaml"

# PROV properties are not classes; the manifest stores them as relation names on a prov Entity.
RELATIONS = ("wasDerivedFrom", "wasAttributedTo")


@lru_cache(maxsize=1)
def _class_uris() -> tuple[str, ...]:
    with PACK_PATH.open(encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    uris: list[str] = []
    for group in ("foundational_ontologies", "domain_ontologies", "customer_ontologies"):
        for onto in pack.get(group) or []:
            uris.extend(c["class_uri"] for c in onto.get("sample_classes") or [])
    return tuple(sorted(set(uris)))


def allowed_uris() -> frozenset[str]:
    """Return every ``class_uri`` listed under any ``sample_classes`` in the pack."""
    return frozenset(_class_uris())


def is_allowed(uri: str) -> bool:
    """Return True when ``uri`` is a listed class."""
    return uri in allowed_uris()


# The brief names these classes, but the pack lists a differently named class for the same concept.
# ``cls`` uses the exact name when the pack lists it, otherwise the stand-in. DCSA's Shipper, Consignee,
# NotifyParty and CargoItem resolve exactly, so they need no entry.
FALLBACKS = {
    # UN/CEFACT latest vocabulary (test.uncefact.org) renamed or dropped these D23B terms.
    "TradeTransaction": "TradeAgreement",
    "SupplyChainTradeLineItem": "TradeItem",
    "SpecifiedInspection": "Assessment",
    "CreditFacility": "LoanOrCreditAccount",
    "LEIRegistration": "LegalEntityIdentifierRegistryEntry",
    "PartyInRole": "PartyRoleIdentifier",
    "SettlementTerms": "DerivativeSettlementTerms",
    # DCSA calls the bill of lading a transport document.
    "BillOfLading": "TransportDocument",
    # ACTUS publishes no ontology; these map to the closest class in the loaded snapshots.
    "CEG": "RBWWm7M917awGxBEiWI5XsG",  # FOLIO "Letter of Credit" (the s3 standby LC)
    "CLM": "cdm.event.common.MarginCallIssuance",  # ISDA CDM margin call
    "SWAPS": "OverTheCounterDerivativeInstrument",
    "FXOUT": "OverTheCounterDerivativeInstrument",
    # FOLIO class IRIs are opaque; these readable names resolve to them.
    "LetterOfCredit": "RBWWm7M917awGxBEiWI5XsG",
    "LetterOfIndemnity": "RB8v1h0OiUgX3iXn0LXUg7g",
}


# Names that more than one listed ontology defines, where the corpus means one of them.
PREFERRED = {"Invoice": "schema.org"}


def _hits(name: str, within: str | None) -> list[str]:
    # Local name after the last "/" or "#", or after the last ":" for URNs such as urn:dcsa:ebl:Shipper.
    hits = [u for u in _class_uris() if u.rsplit("/", 1)[-1].rsplit("#", 1)[-1].rsplit(":", 1)[-1] == name]
    if within:
        hits = [u for u in hits if within.lower() in u.lower()]
    return hits


def cls(name: str, within: str | None = None) -> str:
    """Return the class URI whose local name is ``name``.

    Several ontologies share local names (``LegalEntity`` is in GLEIF and FIBO). Pass ``within``, a
    case-insensitive fragment of the URI such as ``"gleif"``, to choose. Raises ``KeyError`` when no
    listed class matches and when more than one does.
    """
    hits = _hits(name, within)
    if not hits and name in FALLBACKS:
        hits = _hits(FALLBACKS[name], within)
    if not hits:
        raise KeyError(f"class {name!r} is not in the pack's sample_classes")
    if len(hits) > 1 and within is None and name in PREFERRED:
        hits = [u for u in hits if PREFERRED[name] in u]
    if len(hits) > 1:
        raise KeyError(f"class {name!r} is ambiguous, pass within= one of {hits}")
    return hits[0]
