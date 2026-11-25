# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared pieces for the settlement scenarios.

Every payment reads the last cash-fixing post-trade document of its trade through ``last_doc()``: payer, payee,
accounts, amount and due date come from there and are never retyped. Amounts that originate in settlement
(a bank charge, a late-interest rate read from the agreement, a daily price series) are set once in a scenario
module and printed from that variable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ..post_trade.common import (
    Desk,
    account_for,
    business_date,
    deal_of,
    dec,
    due_date,
    fmt,
    form,
    last_doc,
    latest_ticket,
    num,
    onboarded,
    posted,
    q2,
    show,
    traded,
    wait_until,
)
from ..scenario import Scenario

__all__ = [
    "TREASURY_LIMIT",
    "Desk",
    "account_for",
    "bank_mailbox",
    "business_date",
    "deal_of",
    "dec",
    "due_date",
    "fmt",
    "form",
    "last_doc",
    "latest_ticket",
    "num",
    "onboarded",
    "pay_start",
    "payment",
    "posted",
    "q2",
    "register",
    "show",
    "traded",
    "wait_until",
]

TREASURY_LIMIT = Decimal(10_000_000)  # USD, a single release above this needs a second approver


def pay_start(upstream: Any, trade_id: str, days: int = 1) -> datetime:
    """Return the settlement scenario start: ``days`` after the clock of the post-trade scenario of ``trade_id``."""
    for sc in upstream.values():
        if sc.phase == "post_trade" and any(r["trade_id"] == trade_id for r in sc.data.get("documents", [])):
            return sc.clock + timedelta(days=days)
    raise KeyError(f"no post-trade scenario holds documents of {trade_id}")


def payment(u: Any, upstream: Any, trade_id: str) -> dict[str, Any]:
    """Return the cash-fixing post-trade document of ``trade_id`` with ``amount`` as a ``Decimal``."""
    doc = dict(last_doc(trade_id, u, upstream))
    doc["amount"] = dec(doc["payable"])
    return doc


def bank_mailbox(d: Desk, account_id: str) -> str:
    """Return the operations mailbox of the bank that holds ``account_id``."""
    return f"{d.u['accounts'][account_id]['bank']}:operations"


def register(sc: Scenario, **record: Any) -> dict[str, Any]:
    """Append one cash movement to ``sc.data["payments"]``: ``trade_id``, ``amount``, ``currency``, ``value_date``..."""
    sc.data.setdefault("payments", []).append(record)
    return record
