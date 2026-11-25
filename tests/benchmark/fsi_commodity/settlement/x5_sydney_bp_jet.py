# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""x5: BP invoices Sydney for a cargo London dealt. The two NewCo entities sort it out and BP nets it against the Jet A-1 invoice."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .. import ids
from ..ontology import cls
from ..post_trade.docs import invoice, priced
from ..scenario import Scenario
from .common import (
    Desk,
    account_for,
    bank_mailbox,
    deal_of,
    fmt,
    num,
    onboarded,
    pay_start,
    payment,
    register,
    traded,
    wait_until,
)
from .docs import netting_statement, settlement_credit_advice

SCENARIO_ID = "x5-sydney-bp-jet"
TRADE_SCENARIO = "t5-sydney-bp-jet"
EXPECT = {"emails": 6, "attachments": 3, "chats": 0}
LONDON_PRODUCT = "Jet A-1"
LONDON_QUANTITY = 12_000  # mt, bought by NewCo London from BP Oil International
LONDON_PRICE = Decimal("781.50")  # USD per mt


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    pay = payment(u, upstream, deal["trade_id"])
    tid, receivable, currency = pay["trade_id"], pay["amount"], pay["currency"]
    ours, bp_au, due = pay["payee"], pay["payer"], date.fromisoformat(pay["due"])
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    london, bp = onb["london_entity"], onb["parent_counterparty"]
    desk, year = london.split("-")[1], int(str(deal["terms"]["delivery_from"])[2:4])
    taken = [int(k.rsplit("-", 1)[1]) for k in traded(u, upstream) if k.startswith(f"NC-{desk.upper()}-{year}-")]
    london_tid = ids.trade_id(desk, year, max(taken) + 1)
    sc = Scenario(
        SCENARIO_ID,
        "settlement",
        u,
        pay_start(upstream, tid),
        expect=EXPECT,
        title="BP invoices Sydney for a trade London dealt: wrong entity, netting statement under the MPTA, net settlement",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def lon(role: str) -> str:
        return f"{london}:{role}"

    def b(role: str) -> str:
        return f"{bp}:{role}"

    bank = bank_mailbox(d, pay["payee_account"])
    bank_id = bank.split(":")[0]
    line = priced(
        f"{LONDON_PRODUCT}, {LONDON_QUANTITY:,} mt at {LONDON_PRICE:,.2f}", LONDON_QUANTITY, "mt", LONDON_PRICE
    )
    bp_number = f"BPOI-INV-{london_tid}"
    bp_inv, bp_total = invoice(
        d,
        number=bp_number,
        filename=f"{london_tid}-bp-invoice.pdf",
        title=f"Invoice {bp_number}",
        trade_id=london_tid,
        issuer_id=bp,
        bill_to_id=ours,
        issue_date=d.today(),
        due=due,
        lines=[line],
        currency=currency,
        pay_to=account_for(d, bp, currency),
        extra=[("Agreement", onb["agreement"])],
        facts={"inputs": {"quantity": LONDON_QUANTITY, "price": num(LONDON_PRICE)}},
        kind="supplier-invoice",
    )
    d.mail(
        "bp-invoice",
        b("accounting"),
        n("accounting"),
        f"Invoice {bp_number} for USD {fmt(bp_total)} is attached, trade {london_tid}, {LONDON_QUANTITY:,} mt {LONDON_PRODUCT}. Due {due.isoformat()}.",
        bp_inv,
        subject=f"Invoice {bp_number}: {london_tid}",
        tags=["invoice", "planted"],
        doc=cls("Invoice"),
        facts={
            "trade_id": london_tid,
            "invoice_number": bp_number,
            "total_due": num(bp_total),
            "billed_to": d.org(ours)["name"],
        },
    )
    d.mail(
        "ask-london",
        n("accounting"),
        lon("accounting"),
        f"BP has invoiced us USD {fmt(bp_total)} for {london_tid}. We have no deal ticket or confirmation for that trade. Did London deal it?",
        reply="bp-invoice",
        days=1,
        tags=["invoice", "query"],
        facts={"trade_id": london_tid, "invoice_number": bp_number},
    )
    d.mail(
        "london-confirms",
        lon("accounting"),
        n("accounting"),
        f"Yes, {london_tid} is a London trade: {LONDON_QUANTITY:,} mt {LONDON_PRODUCT} bought from BP Oil International. The invoice should be addressed to {d.org(london)['name']}.",
        reply="ask-london",
        days=1,
        tags=["invoice", "query"],
        facts={"trade_id": london_tid, "dealing_entity": london},
    )
    d.mail(
        "wrong-entity-notice",
        n("accounting"),
        b("accounting"),
        f"Invoice {bp_number} is addressed to {d.org(ours)['name']}, but {london_tid} was dealt by {d.org(london)['name']}. Please reissue it to London. BP Australia owes us USD {fmt(receivable)} on {tid}, due {due.isoformat()}. Please net the two under the agreement.",
        reply="bp-invoice",
        days=1,
        tags=["invoice", "dispute", "netting"],
        facts={"trade_id": london_tid, "correct_entity": london, "receivable": num(receivable)},
    )
    items = [
        (f"Tax invoice {pay['number']}, trade {tid}, due from {d.org(bp_au)['name']}", "receivable", receivable),
        (f"Invoice {bp_number}, trade {london_tid}, reissued to {d.org(london)['name']}", "payable", bp_total),
    ]
    net_stmt, net = netting_statement(
        d,
        filename=f"{tid}-netting-statement.pdf",
        issuer_id=bp,
        to_id=ours,
        agreement=onb["agreement"],
        value_date=due,
        items=items,
        currency=currency,
        reference=f"NET {tid} {london_tid}",
    )
    d.mail(
        "netting-statement",
        b("accounting"),
        n("accounting"),
        f"We reissued {bp_number} to {d.org(london)['name']} and netted both items under the agreement. Net USD {fmt(net)} is payable to NewCo on {due.isoformat()}. Statement attached.",
        net_stmt,
        subject=f"Netting statement {tid} / {london_tid}",
        cc=[lon("accounting")],
        days=1,
        tags=["netting", "payment"],
        doc=cls("MonetaryAmount"),
        facts={
            "trade_ids": [tid, london_tid],
            "receivable": num(receivable),
            "payable": num(bp_total),
            "net": num(net),
        },
    )
    wait_until(d, bank, due)
    advice, credited = settlement_credit_advice(
        d,
        filename=f"{tid}-net-credit-advice.pdf",
        trade_id=tid,
        bank_id=bank_id,
        account_id=pay["payee_account"],
        value_date=due,
        instructed=net,
        charges=Decimal(0),
        currency=currency,
        reference=f"NET {tid} {london_tid}",
        originator=d.org(bp_au)["name"],
    )
    d.mail(
        "net-credit-advice",
        bank,
        n("settlements"),
        f"We credited USD {fmt(credited)} to your account on {due.isoformat()}, reference NET {tid} {london_tid}.",
        advice,
        subject=f"Credit advice NET {tid} {london_tid}",
        tags=["payment", "credit-advice", "netting"],
        doc=cls("MonetaryAmount"),
        facts={"trade_id": tid, "amount": num(credited), "value_date": due.isoformat()},
    )
    d.contradiction(
        "invoice-wrong-entity",
        "invoice-vs-trade-record",
        f"BP Oil International invoiced {d.org(ours)['name']} for {london_tid}, which {d.org(london)['name']} dealt. Sydney has no ticket or confirmation for it.",
        [bp_inv.filename, pay["filename"]],
        {"field": "billed_entity", "agreed": london, "found": ours, "trade_id": london_tid},
        True,
        "wrong-entity-notice",
    )
    register(
        sc,
        trade_id=tid,
        amount=num(credited),
        currency=currency,
        value_date=due.isoformat(),
        due=due.isoformat(),
        london_trade_id=london_tid,
        receivable=num(receivable),
        payable=num(bp_total),
    )
    sc.outcome = {
        "result": "netted",
        "trade_id": tid,
        "london_trade_id": london_tid,
        "receivable": num(receivable),
        "payable": num(bp_total),
        "net_settled": num(credited),
        "value_date": due.isoformat(),
    }
    d.question(
        1,
        "reconciliation",
        f"What net amount did BP settle with NewCo Sydney for {tid} and {london_tid}?",
        num(credited),
    )
    d.question(
        2,
        "reconciliation",
        f"Which NewCo entity dealt {london_tid}, which BP invoiced to Sydney?",
        d.org(london)["name"],
    )
    d.question(3, "metadata", f"What was the amount of BP invoice {bp_number}?", num(bp_total))
    return sc
