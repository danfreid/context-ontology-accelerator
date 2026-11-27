# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contract lifecycle ground truth: a clause event timeline, ``terms_as_of`` and the calculations that use terms.

Every change to a clause term is an *event*: who moved it, in which message, through which channel (draft,
tracked-change redline, Word comment, email body, internal instruction, execution, amendment), when it was
recorded and, for amendments, from when it takes effect. ``terms_as_of`` answers both "what was on the table at
that moment" and "what was agreed and in effect on that date", so an "as of" query has a single right answer.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from ..post_trade.common import q2

# Event statuses. ``internal`` is a desk instruction never sent to the counterparty.
OFFER = ("proposed", "countered", "accepted")
BINDING = ("agreed", "executed", "amended")
STATUSES = (*OFFER, *BINDING, "internal")
SOURCES = ("draft", "redline", "comment", "email-body", "internal", "executed", "amendment")


class Timeline:
    """Append-only clause events; ``seq`` orders them."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(
        self,
        clause: str,
        param: str,
        value: Any,
        status: str,
        by: str,
        recorded_at: datetime,
        message: str,
        source: str,
        *,
        attachment: str | None = None,
        sha256: str | None = None,
        effective_from: date | None = None,
    ) -> dict[str, Any]:
        if status not in STATUSES or source not in SOURCES:
            raise ValueError(f"bad status {status!r} or source {source!r}")
        ev = {
            "seq": len(self.events) + 1,
            "clause": clause,
            "param": param,
            "value": value,
            "status": status,
            "by": by,
            "recorded_at": recorded_at.isoformat(),
            "effective_from": effective_from.isoformat() if effective_from else None,
            "message": message,
            "source": source,
            "attachment": attachment,
            "sha256": sha256,
        }
        self.events.append(ev)
        return ev


def terms_as_of(events: Sequence[Mapping[str, Any]], when: datetime, view: str = "agreed") -> dict[str, dict[str, Any]]:
    """Clause terms at ``when``.

    ``view="agreed"``: the latest binding event recorded by ``when`` whose ``effective_from`` (if any) is on or
    before ``when``'s date. ``view="on_table"``: the latest event of any non-internal status recorded by ``when``.
    Returns ``{clause: {param: {"value", "seq", "status"}}}``.
    """
    if view not in ("agreed", "on_table"):
        raise ValueError(view)
    allowed = BINDING if view == "agreed" else (*OFFER, *BINDING)
    out: dict[str, dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: e["seq"]):
        if ev["status"] not in allowed or datetime.fromisoformat(ev["recorded_at"]) > when:
            continue
        if view == "agreed" and ev["effective_from"] and date.fromisoformat(ev["effective_from"]) > when.date():
            continue
        out.setdefault(ev["clause"], {})[ev["param"]] = {"value": ev["value"], "seq": ev["seq"], "status": ev["status"]}
    return out


def value_as_of(
    events: Sequence[Mapping[str, Any]], clause: str, param: str, when: datetime, view: str = "agreed"
) -> Any:
    """One term at ``when`` (see ``terms_as_of``); ``None`` when nothing qualifies."""
    entry = terms_as_of(events, when, view).get(clause, {}).get(param)
    return None if entry is None else entry["value"]


# --- calculations that use agreed terms -------------------------------------------------------------
def add_months(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def renewal_notice_deadline(effective: date, months: int, notice_days: int) -> tuple[date, date]:
    """Return (end of the first term, last day to give notice of non-renewal)."""
    end = add_months(effective, months) - timedelta(days=1)
    return end, end - timedelta(days=notice_days)


def net_settlement(receivable: Decimal, payable: Decimal) -> tuple[Decimal, str]:
    """Net two amounts due the same day in the same currency; returns the amount and who pays (``us``/``them``)."""
    net = receivable - payable
    return (net, "them") if net >= 0 else (-net, "us")


def margin_call(
    exposure: Decimal, threshold: Decimal, min_transfer: Decimal, rounding: Decimal = Decimal(10_000)
) -> Decimal:
    """Credit support due: exposure above the threshold, rounded up to ``rounding``; zero below the MTA."""
    above = exposure - threshold
    if above <= 0:
        return Decimal(0)
    amount = (above / rounding).to_integral_value(rounding=ROUND_CEILING) * rounding
    return amount if amount >= min_transfer else Decimal(0)


def late_interest(amount: Decimal, rate_pct: Decimal, days_late: int) -> Decimal:
    """Simple interest, actual/360, rounded to cents."""
    return q2(amount * rate_pct / 100 * days_late / 360)
