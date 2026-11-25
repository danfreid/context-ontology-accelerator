# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""x4: NewCo New York transfers collateral, then settles the first swap statement. The first statement uses the wrong prices; the corrected one is settled."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .. import ids
from ..calendars import CALENDARS, add_business_days
from ..ontology import cls
from ..scenario import Scenario
from .common import (
    Desk,
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
from .docs import collateral_notice, payment_notice, swap_statement

SCENARIO_ID = "x4-newyork-glencore-isda"
TRADE_SCENARIO = "t4-newyork-glencore-isda"
EXPECT = {"emails": 6, "attachments": 4, "chats": 0}
PRICE_DAYS = 23  # daily Platts assessments in the first calculation period
START_PRICE = Decimal("63.10")  # USD per bbl, the assessment the day before the swap starts
SOURCE = "Platts WTI Cushing"


def _price_series(u: dict[str, Any], count: int) -> list[Decimal]:
    """Return ``count`` + 1 deterministic daily prices: the day before the period, then one per pricing day."""
    rng = ids.rng_for(u["seed"], f"{SCENARIO_ID}:prices")
    out = [START_PRICE]
    for _ in range(count):
        out.append(out[-1] + Decimal(rng.randint(-60, 60)) / 100)
    return out


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    terms = deal["terms"]
    call = payment(u, upstream, deal["trade_id"])
    tid, ours, theirs, currency = call["trade_id"], call["payer"], call["payee"], call["currency"]
    collateral, due = call["amount"], date.fromisoformat(call["due"])
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    fixed, notional = dec(terms["fixed_price_usd_per_bbl"]), int(terms["notional_bbl_per_month"])
    sc = Scenario(
        SCENARIO_ID,
        "settlement",
        u,
        pay_start(upstream, tid),
        expect=EXPECT,
        title="New York transfers collateral and settles the first WTI swap statement, built first from the wrong prices",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    # --- collateral transfer ------------------------------------------------------------------
    wait_until(d, n("settlements"), due)
    value = max(due, d.today())  # the transfer goes out on the call date, or the same day if that has passed
    notice = collateral_notice(
        d,
        filename=f"{tid}-collateral-transfer.pdf",
        trade_id=tid,
        from_id=ours,
        to_id=theirs,
        amount=collateral,
        currency=currency,
        value_date=value,
        margin_call=call["number"],
        csa=onb["csa_agreement"],
        from_account=call["payer_account"],
        to_account=call["payee_account"],
    )
    d.mail(
        "collateral-transfer",
        n("settlements"),
        v("settlements"),
        f"We transferred USD {fmt(collateral)} of cash collateral today against margin call {call['number']} under the CSA. Notice attached.",
        notice,
        subject=f"Collateral transfer {call['number']}",
        tags=["payment", "collateral"],
        doc=cls("CollateralAgreement"),
        facts={
            "trade_id": tid,
            "amount": num(collateral),
            "margin_call": call["number"],
            "value_date": value.isoformat(),
            "call_due": due.isoformat(),
        },
    )
    d.mail(
        "collateral-received",
        v("settlements"),
        n("settlements"),
        f"Received USD {fmt(collateral)} against {call['number']}. Collateral held for {tid} is now USD {fmt(collateral)}.",
        reply="collateral-transfer",
        days=1,
        tags=["payment", "collateral"],
        facts={"trade_id": tid, "amount": num(collateral), "collateral_held": num(collateral)},
    )

    # --- monthly swap statement from 23 daily prices ------------------------------------------
    cal = CALENDARS[d.org(ours)["calendar"]]
    day, days = date.fromisoformat(terms["start"]), []
    for _ in range(PRICE_DAYS):
        days.append(day)
        day = add_business_days(cal, day, 1)
    series = _price_series(u, PRICE_DAYS)
    right = list(zip(days, series[1:], strict=True))
    wrong = list(zip(days, series[:-1], strict=True))  # each day carries the previous day's assessment
    statement_date = business_date(d, ours, days[-1], 1)
    wait_until(d, n("accounting"), statement_date)
    stmt1, r1 = swap_statement(
        d,
        filename=f"{tid}-swap-statement-v1.pdf",
        trade_id=tid,
        version=1,
        agent_id=ours,
        to_id=theirs,
        statement_date=d.today(),
        fixed_price=fixed,
        notional=notional,
        prices=wrong,
        source=SOURCE,
        currency=currency,
    )
    d.mail(
        "swap-statement",
        n("accounting"),
        v("accounting"),
        f"The first calculation period of {tid} ended {days[-1].isoformat()}. Statement attached: average floating price {r1['average']:.4f}, NewCo owes USD {fmt(r1['amount'])}.",
        stmt1,
        subject=f"Swap statement {tid}: first period",
        tags=["swap-statement", "planted"],
        doc=cls("SettlementTerms"),
        facts={"trade_id": tid, "version": 1, "average": num(r1["average"]), "amount": num(r1["amount"])},
    )
    stmt2, r2 = swap_statement(
        d,
        filename=f"{tid}-swap-statement-v2.pdf",
        trade_id=tid,
        version=2,
        agent_id=ours,
        to_id=theirs,
        statement_date=business_date(d, ours, d.today(), 2),
        fixed_price=fixed,
        notional=notional,
        prices=right,
        source=SOURCE,
        currency=currency,
        note="Corrected: each pricing day now carries its own assessment. Version 1 used the previous day's assessment.",
    )
    if r2["amount"] <= 0:
        raise ValueError("the fixed payer must owe a positive amount; change START_PRICE")
    d.mail(
        "statement-query",
        v("accounting"),
        n("accounting"),
        f"Our desk's {SOURCE} assessments do not match your statement. Each of your {PRICE_DAYS} prices equals the previous day's assessment. By our figures the average is {r2['average']:.4f}, so the amount is USD {fmt(r2['amount'])}, not USD {fmt(r1['amount'])}.",
        reply="swap-statement",
        days=1,
        tags=["swap-statement", "dispute"],
        facts={
            "trade_id": tid,
            "average": num(r2["average"]),
            "amount": num(r2["amount"]),
            "statement_amount": num(r1["amount"]),
        },
    )
    d.mail(
        "statement-corrected",
        n("accounting"),
        v("accounting"),
        f"You are right. Version 2 is attached with each day's own assessment: average {r2['average']:.4f}, NewCo owes USD {fmt(r2['amount'])}.",
        stmt2,
        reply="statement-query",
        days=1,
        tags=["swap-statement", "correction"],
        doc=cls("SettlementTerms"),
        facts={"trade_id": tid, "version": 2, "average": num(r2["average"]), "amount": num(r2["amount"])},
    )
    pay_date = business_date(d, ours, d.today(), 2)
    wait_until(d, n("settlements"), pay_date)
    pay_from = next(
        k
        for k in sorted(u["accounts"])
        if u["accounts"][k]["owner"] == ours
        and u["accounts"][k]["currency"] == currency
        and u["accounts"][k]["purpose"] == "operating"
    )
    reference = f"{tid} {SCENARIO_ID.split('-')[0].upper()} P1"
    pay_notice = payment_notice(
        d,
        filename=f"{tid}-swap-payment-notice.pdf",
        kind="payment-notice",
        title=f"Payment notice: {reference}",
        trade_id=tid,
        payer_id=ours,
        payee_id=theirs,
        value_date=pay_date,
        amount=r2["amount"],
        currency=currency,
        reference=reference,
        from_account=pay_from,
        to_account=call["payee_account"],
        covers=[("Settles swap statement", f"{tid} version 2")],
    )
    d.mail(
        "swap-payment",
        n("settlements"),
        v("settlements"),
        f"We paid USD {fmt(r2['amount'])} today, the amount on swap statement version 2 for {tid}. Payment notice attached.",
        pay_notice,
        subject=f"Swap settlement {reference}",
        tags=["payment", "payment-notice", "swap-statement"],
        doc=cls("MonetaryAmount"),
        facts={
            "trade_id": tid,
            "amount": num(r2["amount"]),
            "value_date": pay_date.isoformat(),
            "statement_version": 2,
        },
    )
    d.contradiction(
        "statement-wrong-prices",
        "statement-vs-prices",
        f"Swap statement v1 uses the previous day's assessment for each of the {PRICE_DAYS} pricing days: average {r1['average']:.4f} and USD {fmt(r1['amount'])}, against {r2['average']:.4f} and USD {fmt(r2['amount'])} from the right prices.",
        [stmt1.filename, stmt2.filename],
        {"field": "amount", "agreed": num(r2["amount"]), "found": num(r1["amount"]), "price_count": PRICE_DAYS},
        True,
        "statement-query",
    )
    register(
        sc,
        trade_id=tid,
        amount=num(r2["amount"]),
        currency=currency,
        value_date=pay_date.isoformat(),
        collateral=num(collateral),
        first_statement_amount=num(r1["amount"]),
    )
    sc.outcome = {
        "result": "settled-after-correction",
        "trade_id": tid,
        "collateral_transferred": num(collateral),
        "price_count": PRICE_DAYS,
        "first_statement_amount": num(r1["amount"]),
        "settled_amount": num(r2["amount"]),
        "average_floating": num(r2["average"]),
    }
    d.question(1, "reconciliation", f"What amount settled on the first swap statement for {tid}?", num(r2["amount"]))
    d.question(
        2,
        "reconciliation",
        f"What average floating price did the corrected statement for {tid} use?",
        num(r2["average"]),
    )
    d.question(3, "metadata", f"How many daily prices does the swap statement for {tid} list?", PRICE_DAYS)
    return sc
