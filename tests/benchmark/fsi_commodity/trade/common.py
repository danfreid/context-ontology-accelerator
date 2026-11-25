# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared pieces for the trade scenarios: credit arithmetic, deal records, ticket documents, ``traded(u)``.

Every value that originates in onboarding (agreement name, payment days, tolerance, governing law, credit
limit and exposure, effective date, accounts) is read through ``onboarded(u)``. Values that originate in a
deal (quantity, price, dates) are set once in the scenario module and printed from that variable.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from ..calendars import CALENDARS, add_business_days
from ..onboarding.common import Desk, form, money, onboarded, slug
from ..ontology import cls
from ..scenario import Attachment, Scenario

__all__ = [
    "TRADE_YEAR",
    "Desk",
    "business_date",
    "credit_check",
    "credit_check_pdf",
    "deal_doc",
    "deal_record",
    "finish",
    "form",
    "latest_ticket",
    "money",
    "num",
    "onboarded",
    "record_mismatch",
    "record_ticket",
    "show",
    "slug",
    "start_clock",
    "traded",
]

TRADE_YEAR = 2026


def num(value: Decimal | int | float) -> int | float:
    """Return ``value`` as an int when it is whole, otherwise as a float (for JSON and display)."""
    dec = Decimal(str(value))
    return int(dec) if dec == dec.to_integral_value() else float(dec)


def show(key: str, value: Any) -> str:
    """Format a term for a document: thousands separators, 2 decimals for prices, 4 for rates."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}" if key.endswith("rate") else f"{value:.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def start_clock(onb: Mapping[str, Any], days: int = 14) -> datetime:
    """Return the trade scenario start: ``days`` after the onboarding scenario's effective date."""
    return datetime.combine(date.fromisoformat(onb["effective"]) + timedelta(days=days), time(14, 0), tzinfo=UTC)


def business_date(d: Desk, org_id: str, start: date, n: int) -> date:
    """Return the date ``n`` business days after ``start`` in ``org_id``'s calendar."""
    return add_business_days(CALENDARS[d.org(org_id)["calendar"]], start, n)


# --- credit -------------------------------------------------------------------------------------


def credit_check(
    onb: Mapping[str, Any],
    value: int,
    *,
    group_limit: int | None = None,
    other_group_exposure: int = 0,
    existing: int = 0,
) -> dict[str, Any]:
    """Credit arithmetic for one deal from the onboarded limit and exposure.

    ``existing`` is exposure already booked since onboarding (an earlier deal). A counterparty whose
    onboarding is secured-only has no open limit, so the deal needs prepayment and uses no credit.
    """
    credit = onb["credit"]
    limit = credit["limit"]
    before = credit["exposure"] + existing
    booked = before + value
    if limit == 0 and onb.get("prepayment_only"):
        status = "prepayment_required"
    elif booked > limit or (group_limit is not None and other_group_exposure + booked > group_limit):
        status = "declined"
    else:
        status = "approved"
    used = value if status == "approved" else 0
    after = before + used
    out: dict[str, Any] = {
        "limit": limit,
        "exposure_before": before,
        "headroom_before": limit - before,
        "deal_value": value,
        "exposure_if_booked": booked,
        "status": status,
        "credit_used": used,
        "exposure_after": after,
        "headroom_after": limit - after,
    }
    if group_limit is not None:
        out |= {
            "group_limit": group_limit,
            "other_group_exposure": other_group_exposure,
            "group_exposure_before": other_group_exposure + before,
            "group_exposure_after": other_group_exposure + after,
            "group_headroom_after": group_limit - other_group_exposure - after,
        }
    return out


def credit_check_pdf(
    d: Desk, ours: str, cp: str, trade_id: str, check: Mapping[str, Any], *, suffix: str = ""
) -> Attachment:
    """The credit check sheet a credit analyst sends with a decision."""
    pairs = [
        ("Trade id", trade_id),
        ("Counterparty", d.org(cp)["name"]),
        ("Booking entity", d.org(ours)["name"]),
        ("Deal value (USD)", money(check["deal_value"])),
        ("Approved limit (USD)", money(check["limit"])),
        ("Exposure before (USD)", money(check["exposure_before"])),
        ("Exposure if booked (USD)", money(check["exposure_if_booked"])),
        ("Headroom before (USD)", money(check["headroom_before"])),
    ]
    if "group_limit" in check:
        pairs += [
            ("Group limit (USD)", money(check["group_limit"])),
            ("Group exposure before (USD)", money(check["group_exposure_before"])),
            ("Group exposure if booked (USD)", money(check["other_group_exposure"] + check["exposure_if_booked"])),
        ]
    pairs.append(("Decision", check["status"].replace("_", " ")))
    return form(
        f"{trade_id}-credit-check{suffix}.pdf",
        "credit-check",
        f"Credit check: {trade_id}",
        pairs,
        cls("CreditFacility"),
        {"trade_id": trade_id, "counterparty": d.org(cp)["name"], **check},
    )


# --- documents ----------------------------------------------------------------------------------


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def deal_doc(
    d: Desk,
    *,
    kind: str,
    filename: str,
    title: str,
    issuer: str,
    trade_id: str,
    ours: str,
    cp: str,
    terms: Mapping[str, Any],
    class_uri: str,
    version: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Attachment:
    """A deal ticket or recap PDF: the terms as printed, plus the onboarding terms the deal rests on."""
    extra = dict(extra or {})
    pairs = [("Trade id", trade_id)]
    if version is not None:
        pairs.append(("Version", str(version)))
    pairs += [("Issued by", issuer), ("Booking entity", d.org(ours)["name"]), ("Counterparty", d.org(cp)["name"])]
    pairs += [(_label(k), show(k, v)) for k, v in terms.items()]
    pairs += [(_label(k), show(k, v)) for k, v in extra.items()]
    facts: dict[str, Any] = {"trade_id": trade_id, "issued_by": issuer, **terms, **extra}
    if version is not None:
        facts["version"] = version
    return form(filename, kind, title, pairs, class_uri, facts)


# --- deal records -------------------------------------------------------------------------------


def deal_record(
    sc: Scenario,
    onb: Mapping[str, Any],
    *,
    trade_id: str,
    kind: str,
    side: str,
    terms: Mapping[str, Any],
    notional: int,
    credit: Mapping[str, Any] | None,
    class_uris: Mapping[str, str],
    status: str = "agreed",
    **extra: Any,
) -> dict[str, Any]:
    """Build one manifest deal. Onboarding values are copied from ``onb``, never typed again."""
    return {
        "trade_id": trade_id,
        "scenario": sc.id,
        "onboarding_scenario": onb["scenario"],
        "entity": onb["entity"],
        "counterparty": onb["counterparty"],
        "kind": kind,
        "side": side,
        "agreement": onb["agreement"],
        "payment_days": onb["payment_days"],
        "tolerance_pct": onb["tolerance_pct"],
        "governing_law": onb["governing_law"],
        "agreement_effective": onb["effective"],
        "terms": dict(terms),
        "notional": notional,
        "credit_check": dict(credit) if credit is not None else None,
        "status": status,
        "class_uris": dict(class_uris),
        "tickets": [],
        "mismatches": [],
        **extra,
    }


def record_ticket(deal: dict[str, Any], message: str, att: Attachment) -> None:
    """Append a ticket (or bank confirmation) version; the last one is what later phases read."""
    deal["tickets"].append(
        {
            "version": att.facts.get("version"),
            "message": message,
            "filename": att.filename,
            "issued_by": att.facts["issued_by"],
            "facts": dict(att.facts),
        }
    )


def record_mismatch(
    d: Desk,
    deal: dict[str, Any],
    ident: str,
    kind: str,
    field: str,
    agreed: Any,
    found: Any,
    where: Sequence[str],
    description: str,
    caught: bool,
    caught_by: str | None = None,
) -> None:
    """Record a planted problem on the deal and in the scenario's ``expected_contradictions``."""
    deal["mismatches"].append(
        {
            "id": f"{d.sc.id}:{ident}",
            "type": kind,
            "field": field,
            "agreed": agreed,
            "found": found,
            "where": list(where),
            "caught": caught,
            "caught_by": caught_by,
        }
    )
    d.contradiction(
        ident, kind, description, where, {"field": field, "agreed": agreed, "found": found}, caught, caught_by
    )


def finish(sc: Scenario, deals: Sequence[dict[str, Any]]) -> None:
    """Store the deals on the scenario manifest and in ``data`` for ``traded(u)``."""
    sc.deals.extend(deals)
    sc.data["deals"] = sc.deals


# --- accessors ----------------------------------------------------------------------------------


def traded(u: Mapping[str, Any], upstream: Mapping[str, Scenario] | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{trade id: deal}`` for the trade phase, reading ``upstream`` or building it in memory."""
    if upstream is None:
        from ..onboarding import ONBOARDING
        from . import TRADE

        built: dict[str, Scenario] = {}
        for builder in (*ONBOARDING, *TRADE):
            sc = builder(dict(u), built)
            built = {**built, sc.id: sc}
        upstream = built
    return {deal["trade_id"]: deal for sc in upstream.values() if sc.phase == "trade" for deal in sc.deals}


def latest_ticket(
    trade_id: str, u: Mapping[str, Any], upstream: Mapping[str, Scenario] | None = None
) -> dict[str, Any]:
    """Return the last ticket version of ``trade_id``: ``version``, ``message``, ``filename``, ``facts``."""
    return traded(u, upstream)[trade_id]["tickets"][-1]
