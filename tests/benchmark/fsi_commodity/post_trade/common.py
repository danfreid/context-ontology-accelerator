# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared pieces for the post-trade scenarios: decimal helpers, clock helpers, ``posted(u)`` and ``last_doc()``.

Every deal value (trade id, quantity, price, premium, dates, payment days, tolerance, accounts, the planted
mismatch of the trade phase) is read through ``traded(u)``; amounts that originate in post-trade (certified
quantities, assessments, FX rates) are set once in a scenario module and printed from that variable.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..calendars import CALENDARS, add_business_days, next_business_day
from ..onboarding.common import Desk, form, money, onboarded, slug
from ..scenario import Scenario
from ..trade.common import business_date, latest_ticket, num, show, traded

__all__ = [
    "Desk",
    "account_for",
    "business_date",
    "deal_of",
    "dec",
    "due_date",
    "fmt",
    "form",
    "last_doc",
    "latest_ticket",
    "money",
    "num",
    "onboarded",
    "planted",
    "posted",
    "q2",
    "register",
    "show",
    "slug",
    "trade_start",
    "traded",
    "wait_until",
]

CENT = Decimal("0.01")


def dec(value: Any) -> Decimal:
    """Return ``value`` as a ``Decimal`` through its string form (so ``0.85`` is exactly 0.85)."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q2(value: Decimal) -> Decimal:
    """Round to cents, half up."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value: Decimal | int, places: int = 2) -> str:
    """Thousands separators and a fixed number of decimals."""
    return f"{value:,.{places}f}"


def trade_start(upstream: Mapping[str, Scenario], deal: Mapping[str, Any], days: int = 1) -> datetime:
    """Return the post-trade scenario start: ``days`` after the clock of the trade scenario that booked ``deal``."""
    return upstream[deal["scenario"]].clock + timedelta(days=days)


def wait_until(d: Desk, ref: str, target: date) -> None:
    """Advance the clock to ``target`` (rolled to a business day of ``ref``'s organisation) when it is later."""
    cal = CALENDARS[d.org(ref.split(":")[0])["calendar"]]
    target = next_business_day(cal, target)
    day, n = d.today(), 0
    while day < target:
        day = add_business_days(cal, day, 1)
        n += 1
    if n:
        d.sc.wait_business_days(d.addr(ref), n)


def due_date(d: Desk, org_id: str, start: date, days: int) -> date:
    """Return ``start`` plus ``days`` calendar days, rolled forward to a business day of ``org_id``."""
    return next_business_day(CALENDARS[d.org(org_id)["calendar"]], start + timedelta(days=days))


def account_for(d: Desk, org_id: str, currency: str, prefer: str | None = None) -> str:
    """Return the id of ``org_id``'s ``currency`` account in the universe; ``prefer`` wins when it matches."""
    accounts = d.u["accounts"]
    if prefer in accounts and accounts[prefer]["currency"] == currency and accounts[prefer]["owner"] == org_id:
        return prefer  # type: ignore[return-value]
    for key in sorted(accounts):
        if accounts[key]["owner"] == org_id and accounts[key]["currency"] == currency:
            return key
    raise KeyError(f"{org_id} has no {currency} account")


def deal_of(u: Mapping[str, Any], upstream: Mapping[str, Scenario], scenario_id: str, n: int = 0) -> dict[str, Any]:
    """Return the ``n``-th deal booked by trade scenario ``scenario_id`` (read from the trade phase, never retyped)."""
    return [x for x in traded(u, upstream).values() if x["scenario"] == scenario_id][n]


def planted(deal: Mapping[str, Any], suffix: str) -> Mapping[str, Any]:
    """Return the trade-phase mismatch of ``deal`` whose id ends with ``suffix``."""
    return next(m for m in deal["mismatches"] if m["id"].endswith(suffix))


# --- document register ---------------------------------------------------------------------------
def register(sc: Scenario, **record: Any) -> dict[str, Any]:
    """Append one post-trade document to ``sc.data["documents"]``; ``posted`` and ``last_doc`` read them.

    Keys used: ``trade_id``, ``kind``, ``filename``, ``message``, ``number``, ``issuer``, ``payer``, ``payee``,
    ``currency``, ``payable`` (the net cash amount due after this document, or ``None``), ``due``,
    ``payer_account``, ``payee_account`` and ``inputs``.
    """
    record.setdefault("payable", None)
    sc.data.setdefault("documents", []).append(record)
    return record


def posted(u: Mapping[str, Any], upstream: Mapping[str, Scenario] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return ``{trade id: [documents in order]}`` for the post-trade phase (built in memory when no ``upstream``)."""
    if upstream is None:
        from ..onboarding import ONBOARDING
        from ..trade import TRADE
        from . import POST_TRADE

        built: dict[str, Scenario] = {}
        for builder in (*ONBOARDING, *TRADE, *POST_TRADE):
            sc = builder(dict(u), built)
            built = {**built, sc.id: sc}
        upstream = built
    out: dict[str, list[dict[str, Any]]] = {}
    for sc in upstream.values():
        if sc.phase == "post_trade":
            for rec in sc.data.get("documents", []):
                out.setdefault(rec["trade_id"], []).append(rec)
    return out


def last_doc(trade_id: str, u: Mapping[str, Any], upstream: Mapping[str, Scenario] | None = None) -> dict[str, Any]:
    """Return the last document of ``trade_id`` that fixes a cash amount (``payable`` is not ``None``)."""
    docs = [r for r in posted(u, upstream)[trade_id] if r["payable"] is not None]
    return docs[-1]
