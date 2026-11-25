# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""x3: Harbourline's payment arrives USD 45.00 short because a bank deducted a charge. The agreement allows no deductions; Harbourline tops up."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from ..clauses import render
from ..ontology import cls
from ..scenario import Scenario
from .common import (
    Desk,
    bank_mailbox,
    business_date,
    deal_of,
    fmt,
    num,
    onboarded,
    pay_start,
    payment,
    register,
    wait_until,
)
from .docs import cash_reconciliation, payment_notice, settlement_credit_advice

SCENARIO_ID = "x3-sydney-harbourline-ulp"
TRADE_SCENARIO = "t3-sydney-harbourline-ulp"
EXPECT = {"emails": 8, "attachments": 4, "chats": 0}
BANK_CHARGE = Decimal("45.00")  # USD, taken from the remittance by the paying bank


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    pay = payment(u, upstream, deal["trade_id"])
    tid, due_amount, currency = pay["trade_id"], pay["amount"], pay["currency"]
    ours, theirs = pay["payee"], pay["payer"]
    due = date.fromisoformat(pay["due"])
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    clause = render("no_deductions", onb["clauses"]["no_deductions"])
    sc = Scenario(
        SCENARIO_ID,
        "settlement",
        u,
        pay_start(upstream, tid),
        expect=EXPECT,
        title="Harbourline pays USD 45.00 short after a bank charge: reconciliation, query against the no-deductions clause, top-up",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    bank = bank_mailbox(d, pay["payee_account"])
    bank_id = bank.split(":")[0]
    reference = f"{tid} {pay['number']}"
    wait_until(d, v("accounting"), due)
    remit = payment_notice(
        d,
        filename=f"{tid}-remittance-advice.pdf",
        kind="remittance-advice",
        title=f"Remittance advice: {reference}",
        trade_id=tid,
        payer_id=theirs,
        payee_id=ours,
        value_date=due,
        amount=due_amount,
        currency=currency,
        reference=reference,
        from_account=pay["payer_account"],
        to_account=pay["payee_account"],
        covers=[(f"Settles credit note {pay['number']}", f"net {currency} {fmt(due_amount)}")],
    )
    d.mail(
        "remittance",
        v("accounting"),
        n("accounting"),
        f"We instructed USD {fmt(due_amount)} today for {tid}, the net payable on credit note {pay['number']}. Remittance advice attached.",
        remit,
        subject=f"Remittance advice {reference}",
        tags=["payment", "remittance"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(due_amount), "value_date": due.isoformat()},
    )
    wait_until(d, bank, due)
    advice, credited = settlement_credit_advice(
        d,
        filename=f"{tid}-credit-advice.pdf",
        trade_id=tid,
        bank_id=bank_id,
        account_id=pay["payee_account"],
        value_date=due,
        instructed=due_amount,
        charges=BANK_CHARGE,
        currency=currency,
        reference=reference,
        originator=d.org(theirs)["name"],
        charge_note="Charge taken by the remitting bank from the transferred amount",
    )
    d.mail(
        "credit-advice",
        bank,
        n("settlements"),
        f"We credited USD {fmt(credited)} to your account on {due.isoformat()}, reference {reference}. Charges of USD {fmt(BANK_CHARGE)} were deducted from the transfer.",
        advice,
        subject=f"Credit advice {reference}",
        tags=["payment", "credit-advice", "short-payment"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "instructed": num(due_amount), "charges": num(BANK_CHARGE), "amount": num(credited)},
    )
    recon, short = cash_reconciliation(
        d,
        filename=f"{tid}-cash-reconciliation.pdf",
        trade_id=tid,
        prepared_by=d.person(n("settlements"))["name"],
        rows=[(f"Net payable on {pay['number']}", due_amount, credited)],
        currency=currency,
        status="open: short payment",
    )
    d.mail(
        "reconciliation",
        n("settlements"),
        n("accounting"),
        f"The cash for {tid} is short by USD {fmt(short)}: expected USD {fmt(due_amount)}, received USD {fmt(credited)}. The credit advice shows a bank charge. Reconciliation attached.",
        recon,
        reply="credit-advice",
        days=0,
        tags=["reconciliation", "short-payment"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "expected": num(due_amount), "received": num(credited), "difference": num(short)},
    )
    d.mail(
        "short-payment-query",
        n("accounting"),
        v("accounting"),
        f'Your payment for {tid} arrived USD {fmt(short)} short. A bank charge was deducted from the transfer. The agreement says: "{clause}" Please send the USD {fmt(short)} difference.',
        reply="remittance",
        days=1,
        tags=["payment", "short-payment", "query"],
        facts={"trade_id": tid, "shortfall": num(short), "clause": "no_deductions"},
    )
    d.mail(
        "short-payment-reply",
        v("accounting"),
        n("accounting"),
        f"Understood. Our bank took USD {fmt(BANK_CHARGE)} from the transfer. We will send the USD {fmt(short)} difference on the next business day.",
        reply="short-payment-query",
        days=1,
        tags=["payment", "short-payment"],
        facts={"trade_id": tid, "shortfall": num(short), "top_up_promised": True},
    )
    topup_date = business_date(d, ours, d.today(), 1)
    wait_until(d, bank, topup_date)
    topup, topup_credited = settlement_credit_advice(
        d,
        filename=f"{tid}-top-up-credit-advice.pdf",
        trade_id=tid,
        bank_id=bank_id,
        account_id=pay["payee_account"],
        value_date=topup_date,
        instructed=short,
        charges=Decimal(0),
        currency=currency,
        reference=f"{reference} TOP-UP",
        originator=d.org(theirs)["name"],
    )
    d.mail(
        "top-up-credit-advice",
        bank,
        n("settlements"),
        f"We credited USD {fmt(topup_credited)} to your account on {topup_date.isoformat()}, reference {reference} TOP-UP.",
        topup,
        subject=f"Credit advice {reference} TOP-UP",
        tags=["payment", "credit-advice", "top-up"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(topup_credited), "value_date": topup_date.isoformat()},
    )
    d.mail(
        "reconciled",
        n("settlements"),
        n("accounting"),
        f"The top-up of USD {fmt(topup_credited)} clears the difference. Received in total USD {fmt(credited + topup_credited)} against USD {fmt(due_amount)} expected. {tid} is reconciled.",
        reply="reconciliation",
        days=0,
        tags=["reconciliation"],
        facts={
            "trade_id": tid,
            "received_total": num(credited + topup_credited),
            "expected": num(due_amount),
            "difference": 0,
        },
    )
    d.mail(
        "closed",
        n("accounting"),
        v("accounting"),
        f"Thank you. We received the USD {fmt(topup_credited)} top-up and the account for {tid} is settled in full.",
        reply="short-payment-reply",
        days=1,
        tags=["payment"],
    )
    d.contradiction(
        "bank-charge-short-payment",
        "payment-vs-invoice",
        f"The credit of USD {fmt(credited)} is USD {fmt(short)} less than the net payable of USD {fmt(due_amount)}: a bank charge was deducted although the agreement allows no deductions.",
        [pay["filename"], advice.filename, onb["agreement"]],
        {"field": "amount", "agreed": num(due_amount), "found": num(credited), "bank_charge": num(BANK_CHARGE)},
        True,
        "short-payment-query",
    )
    register(
        sc,
        trade_id=tid,
        amount=num(credited + topup_credited),
        currency=currency,
        value_date=topup_date.isoformat(),
        due=due.isoformat(),
        first_credit=num(credited),
        top_up=num(topup_credited),
    )
    sc.outcome = {
        "result": "settled-after-top-up",
        "trade_id": tid,
        "net_payable": num(due_amount),
        "first_credit": num(credited),
        "shortfall": num(short),
        "top_up": num(topup_credited),
    }
    d.question(1, "reconciliation", f"By how much was the first payment for {tid} short?", num(short))
    d.question(
        2,
        "hybrid_retrieval",
        f"Which clause did the {tid} bank charge breach?",
        "no deductions, including bank charges",
    )
    d.question(3, "reconciliation", f"What did Harbourline credit in total for {tid}?", num(credited + topup_credited))
    return sc
