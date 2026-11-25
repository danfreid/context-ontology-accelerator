# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""x2: Kestrel pays the final VLSFO balance two days late. NewCo London reminds, matches the credit and waives the late interest."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..calendars import CALENDARS, next_business_day
from ..clauses import render
from ..ontology import cls
from ..scenario import Scenario
from .common import (
    Desk,
    bank_mailbox,
    business_date,
    deal_of,
    dec,
    fmt,
    num,
    onboarded,
    pay_start,
    payment,
    register,
    wait_until,
)
from .docs import account_statement, late_interest_statement, settlement_credit_advice

SCENARIO_ID = "x2-london-kestrel-vlsfo"
TRADE_SCENARIO = "t2-london-kestrel-vlsfo"
EXPECT = {"emails": 6, "attachments": 3, "chats": 0}
DAYS_LATE = 2  # calendar days after the due date that the bank credits the payment


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    pay = payment(u, upstream, deal["trade_id"])
    tid, balance, currency = pay["trade_id"], pay["amount"], pay["currency"]
    ours, theirs = pay["payee"], pay["payer"]  # NewCo London is paid by Kestrel
    due = date.fromisoformat(pay["due"])
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    rate_pct = dec(onb["clauses"]["late_interest"]["rate"])
    sc = Scenario(
        SCENARIO_ID,
        "settlement",
        u,
        pay_start(upstream, tid),
        expect=EXPECT,
        title="Kestrel pays the VLSFO balance two days late: reminder, credit matched, late interest waived",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    cal = CALENDARS[d.org(ours)["calendar"]]
    bank = bank_mailbox(d, pay["payee_account"])
    bank_id = bank.split(":")[0]
    reference = f"{tid} {pay['number']}"
    reminder_date = business_date(d, ours, due, 1)
    wait_until(d, n("accounting"), reminder_date)
    stmt = account_statement(
        d,
        filename=f"{tid}-reminder-statement.pdf",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        invoice_number=pay["number"],
        balance=balance,
        due=due,
        as_of=d.today(),
        currency=currency,
        pay_to=pay["payee_account"],
    )
    d.mail(
        "reminder",
        n("accounting"),
        v("accounting"),
        f"Invoice {pay['number']} for {tid} fell due on {due.isoformat()} and the balance of USD {fmt(balance)} has not reached our account. A statement is attached. Please pay today.",
        stmt,
        subject=f"Reminder: {pay['number']} USD {fmt(balance)} overdue",
        tags=["payment", "reminder"],
        doc=cls("Invoice"),
        facts={
            "trade_id": tid,
            "balance": num(balance),
            "due": due.isoformat(),
            "reminder_date": d.today().isoformat(),
        },
    )
    paid = next_business_day(cal, due + timedelta(days=DAYS_LATE))
    d.mail(
        "reminder-reply",
        v("accounting"),
        n("accounting"),
        f"Sorry for the delay. The payment run releases USD {fmt(balance)} on {paid.isoformat()}.",
        reply="reminder",
        days=1,
        tags=["payment", "reminder"],
        facts={"trade_id": tid, "promised_date": paid.isoformat()},
    )
    wait_until(d, bank, paid)
    advice, credited = settlement_credit_advice(
        d,
        filename=f"{tid}-credit-advice.pdf",
        trade_id=tid,
        bank_id=bank_id,
        account_id=pay["payee_account"],
        value_date=paid,
        instructed=balance,
        charges=dec(0),
        currency=currency,
        reference=reference,
        originator=d.org(theirs)["name"],
    )
    d.mail(
        "credit-advice",
        bank,
        n("settlements"),
        f"We credited USD {fmt(credited)} to your account on {paid.isoformat()}, reference {reference}.",
        advice,
        subject=f"Credit advice {reference}",
        tags=["payment", "credit-advice", "late-payment"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(credited), "value_date": paid.isoformat(), "due": due.isoformat()},
    )
    days_late = (paid - due).days
    interest_stmt, interest = late_interest_statement(
        d,
        filename=f"{tid}-late-interest.pdf",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        balance=balance,
        rate_pct=rate_pct,
        due=due,
        paid=paid,
        currency=currency,
        clause=render("late_interest", onb["clauses"]["late_interest"]),
        waived=True,
    )
    d.mail(
        "payment-matched",
        n("settlements"),
        n("accounting"),
        f"The credit of USD {fmt(credited)} matches {pay['number']}. It arrived {days_late} days after the due date. Interest under the late-payment clause at {rate_pct}% a year is USD {fmt(interest)}.",
        reply="credit-advice",
        days=0,
        tags=["payment", "late-payment"],
        facts={"trade_id": tid, "days_late": days_late, "interest": num(interest), "amount": num(credited)},
    )
    d.mail(
        "interest-waived",
        n("accounting"),
        v("accounting"),
        f"We received USD {fmt(credited)}, {days_late} days after the due date. The late interest of USD {fmt(interest)} is waived. The statement is attached.",
        interest_stmt,
        subject=f"Late interest waived: {tid}",
        days=1,
        tags=["payment", "late-payment", "waiver"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "interest": num(interest), "waived": True, "days_late": days_late},
    )
    d.mail(
        "thanks",
        v("accounting"),
        n("accounting"),
        f"Thank you for waiving the interest on {tid}. We will pay on time next cargo.",
        reply="interest-waived",
        days=1,
        tags=["payment"],
    )
    d.contradiction(
        "late-payment",
        "payment-vs-due-date",
        f"Kestrel's USD {fmt(balance)} was credited on {paid.isoformat()}, {days_late} days after the due date of {due.isoformat()}.",
        [pay["filename"], advice.filename],
        {"field": "value_date", "agreed": due.isoformat(), "found": paid.isoformat(), "days_late": days_late},
        True,
        "payment-matched",
    )
    register(
        sc,
        trade_id=tid,
        amount=num(balance),
        currency=currency,
        value_date=paid.isoformat(),
        due=due.isoformat(),
        days_late=days_late,
        interest=num(interest),
        interest_waived=True,
    )
    sc.outcome = {
        "result": "settled-late",
        "trade_id": tid,
        "amount_paid": num(balance),
        "days_late": days_late,
        "interest_accrued": num(interest),
        "interest_waived": True,
    }
    d.question(
        1, "reconciliation", f"How many days after the due date did Kestrel pay the balance for {tid}?", days_late
    )
    d.question(
        2, "reconciliation", f"What late interest accrued on the {tid} balance before it was waived?", num(interest)
    )
    d.question(3, "metadata", f"What balance did the reminder for {tid} claim?", num(balance))
    return sc
