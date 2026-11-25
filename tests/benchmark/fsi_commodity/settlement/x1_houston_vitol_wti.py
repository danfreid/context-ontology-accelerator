# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""x1 (control): NewCo Houston pays Vitol. The release is above the treasury limit, so a second approver signs. Nothing is planted."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from .common import (
    TREASURY_LIMIT,
    Desk,
    bank_mailbox,
    business_date,
    deal_of,
    fmt,
    num,
    pay_start,
    payment,
    register,
    wait_until,
)
from .docs import debit_advice, payment_notice, release_request

SCENARIO_ID = "x1-houston-vitol-wti"
TRADE_SCENARIO = "t1-houston-vitol-wti"
EXPECT = {"emails": 5, "attachments": 3, "chats": 0}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    pay = payment(u, upstream, deal_of(u, upstream, TRADE_SCENARIO)["trade_id"])
    tid, amount, currency = pay["trade_id"], pay["amount"], pay["currency"]
    ours, theirs = pay["payer"], pay["payee"]
    due = date.fromisoformat(pay["due"])
    sc = Scenario(
        SCENARIO_ID,
        "settlement",
        u,
        pay_start(upstream, tid),
        expect=EXPECT,
        title="Houston pays Vitol: treasury release above the limit, debit advice on the due date, payment notice (control)",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    bank = bank_mailbox(d, pay["payer_account"])
    bank_id = bank.split(":")[0]
    reference = f"{tid} {pay['number']}"
    wait_until(d, n("settlements"), business_date(d, ours, due, -2))
    release = release_request(
        d,
        filename=f"{tid}-release-request.pdf",
        trade_id=tid,
        requested_by=d.person(n("settlements"))["name"],
        approver=d.person(n("accounting"))["name"],
        payee_id=theirs,
        amount=amount,
        currency=currency,
        value_date=due,
        limit=TREASURY_LIMIT,
        from_account=pay["payer_account"],
        to_account=pay["payee_account"],
        reference=reference,
    )
    d.mail(
        "release-request",
        n("settlements"),
        n("accounting"),
        f"Please approve the release of USD {fmt(amount)} to {d.org(theirs)['name']} for {tid}, value {due.isoformat()}. It is above the USD {fmt(TREASURY_LIMIT, 0)} single-release limit, so it needs your second approval.",
        release,
        subject=f"Release approval: {reference}",
        tags=["payment", "treasury"],
        doc=cls("MonetaryAmount"),
        facts={
            "trade_id": tid,
            "amount": num(amount),
            "release_limit": num(TREASURY_LIMIT),
            "value_date": due.isoformat(),
        },
    )
    d.mail(
        "release-approved",
        n("accounting"),
        n("settlements"),
        f"Approved. USD {fmt(amount)} for {tid} may go on {due.isoformat()}. The amount matches the credit note net payable.",
        reply="release-request",
        days=1,
        tags=["payment", "treasury"],
        facts={"trade_id": tid, "amount": num(amount), "approved": True},
    )
    wait_until(d, bank, due)
    debit = debit_advice(
        d,
        filename=f"{tid}-debit-advice.pdf",
        trade_id=tid,
        bank_id=bank_id,
        account_id=pay["payer_account"],
        value_date=due,
        amount=amount,
        currency=currency,
        reference=reference,
        beneficiary=d.org(theirs)["name"],
    )
    d.mail(
        "debit-advice",
        bank,
        n("settlements"),
        f"We debited USD {fmt(amount)} from your account on {due.isoformat()}, reference {reference}.",
        debit,
        subject=f"Debit advice {reference}",
        tags=["payment", "debit-advice"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(amount), "value_date": due.isoformat()},
    )
    notice = payment_notice(
        d,
        filename=f"{tid}-payment-notice.pdf",
        kind="payment-notice",
        title=f"Payment notice: {reference}",
        trade_id=tid,
        payer_id=ours,
        payee_id=theirs,
        value_date=due,
        amount=amount,
        currency=currency,
        reference=reference,
        from_account=pay["payer_account"],
        to_account=pay["payee_account"],
        covers=[(f"Settles credit note {pay['number']}", f"net {currency} {fmt(amount)}")],
    )
    d.mail(
        "payment-notice",
        n("settlements"),
        v("accounting"),
        f"We paid USD {fmt(amount)} today for {tid}, the net payable on credit note {pay['number']}. Payment notice attached.",
        notice,
        subject=f"Payment notice {reference}",
        tags=["payment", "payment-notice"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(amount), "value_date": due.isoformat(), "credit_note": pay["number"]},
    )
    d.mail(
        "receipt",
        v("accounting"),
        n("settlements"),
        f"Received USD {fmt(amount)} for {tid}. The account is settled.",
        reply="payment-notice",
        days=1,
        tags=["payment", "receipt"],
        facts={"trade_id": tid, "amount": num(amount)},
    )
    register(
        sc,
        trade_id=tid,
        amount=num(amount),
        currency=currency,
        value_date=due.isoformat(),
        due=due.isoformat(),
        paid_in_full=True,
    )
    sc.outcome = {
        "result": "settled",
        "trade_id": tid,
        "amount_paid": num(amount),
        "value_date": due.isoformat(),
        "second_approval": amount > TREASURY_LIMIT,
    }
    d.question(1, "reconciliation", f"What amount did NewCo Houston pay Vitol for {tid}?", num(amount))
    d.question(
        2,
        "hybrid_retrieval",
        f"Why did the release for {tid} need a second approver?",
        f"above the USD {int(TREASURY_LIMIT):,} single-release limit",
    )
    d.question(3, "metadata", f"Which bank held the debited account for {tid}?", d.org(bank_id)["name"])
    return sc
