# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""t6: NewCo London buys GBP 4m forward against USD at 1.2845 from Northgate Bank. The bank's first ticket has the wrong value date."""

# ruff: noqa: E501
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .. import ids
from ..ontology import cls
from ..scenario import ChatLine, Scenario
from .common import (
    TRADE_YEAR,
    Desk,
    business_date,
    deal_doc,
    deal_record,
    finish,
    money,
    num,
    record_mismatch,
    record_ticket,
    start_clock,
)

SCENARIO_ID = "t6-london-northgate-gbp-fwd"
EXPECT = {"emails": 6, "attachments": 3, "chats": 1}
TRADE_ID = ids.trade_id("LON", TRADE_YEAR, 2)

OURS, BANK = "newco-lon", "bank-northgate"
GBP_AMOUNT, RATE = 4_000_000, Decimal("1.2845")
FIRST_QUOTE = Decimal("1.2838")
# Northgate is NewCo London's own bank: there is no onboarding scenario, so the deal rests on the bank's dealing terms.
DEALING_TERMS = {
    "scenario": None,
    "entity": OURS,
    "counterparty": BANK,
    "agreement": "Northgate Bank FX dealing terms",
    "payment_days": None,
    "tolerance_pct": None,
    "governing_law": "England and Wales",
    "effective": "2026-03-20",
}


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    onb = dict(DEALING_TERMS)
    sc = Scenario(
        SCENARIO_ID,
        "trade",
        u,
        start_clock(onb),
        expect=EXPECT,
        title="London buys GBP 4m forward from Northgate: the bank's first ticket has the wrong value date",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{OURS}:{role}"

    def b(role: str) -> str:
        return f"{BANK}:{role}"

    me, bank = d.org(OURS), d.org(BANK)
    value_date = business_date(d, OURS, d.today(), 65)
    wrong_date = business_date(d, OURS, value_date, 1)
    usd_amount = int(GBP_AMOUNT * RATE)
    terms = {
        "deal_type": "outright forward",
        "pair": "GBP/USD",
        "buy_currency": "GBP",
        "buy_amount": GBP_AMOUNT,
        "sell_currency": "USD",
        "sell_amount": usd_amount,
        "rate": num(RATE),
        "value_date": value_date.isoformat(),
    }
    txn, deriv, fxout = cls("TradeTransaction"), cls("DerivativeInstrument"), cls("FXOUT")
    extra = {"agreement": onb["agreement"], "governing_law": onb["governing_law"]}
    usd = next(a for a in u["accounts"].values() if a["owner"] == OURS and a["currency"] == "USD")
    gbp = next(a for a in u["accounts"].values() if a["owner"] == OURS and a["currency"] == "GBP")

    def say(role: str, text: str, intent: str, **t: Any) -> ChatLine:
        return ChatLine(d.addr(role), text, intent, t)

    d.sc.chat(
        "dealing",
        [
            say(
                n("trader"),
                f"I need to buy GBP {money(GBP_AMOUNT)} against USD for value {value_date.isoformat()}. Where is the forward?",
                "enquiry",
                pair="GBP/USD",
                buy_currency="GBP",
                buy_amount=GBP_AMOUNT,
                value_date=value_date.isoformat(),
            ),
            say(b("trade_finance"), f"{FIRST_QUOTE}.", "quote", rate=num(FIRST_QUOTE)),
            say(n("trader"), "That has moved against me. Can you do better?", "counter"),
            say(b("trade_finance"), f"{RATE}, last price.", "quote", rate=num(RATE)),
            say(
                n("trader"),
                f"Done. I buy GBP {money(GBP_AMOUNT)} and sell USD {money(usd_amount)} at {RATE}, value {value_date.isoformat()}.",
                "agree",
                **terms,
            ),
        ],
        deal=TRADE_ID,
        tags=["negotiation", "agreed"],
        class_uri=fxout,
    )
    ticket1 = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID}-ticket-v1.pdf",
        title=f"Deal ticket {TRADE_ID}",
        issuer=me["name"],
        trade_id=TRADE_ID,
        ours=OURS,
        cp=BANK,
        terms=terms,
        class_uri=deriv,
        version=1,
        extra=extra,
    )
    d.mail(
        "ticket",
        n("trader"),
        n("ops"),
        f"Deal ticket {TRADE_ID} is attached: buy GBP {money(GBP_AMOUNT)} against USD at {RATE} from {bank['name']}, value {value_date.isoformat()}. Please book it and expect the bank's confirmation.",
        ticket1,
        subject=f"Deal ticket {TRADE_ID}",
        tags=["ticket"],
        doc=deriv,
        facts={"trade_id": TRADE_ID, "ticket_version": 1, "rate": num(RATE), "value_date": value_date.isoformat()},
    )
    ticket2 = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID}-bank-ticket-v2.pdf",
        title=f"Forward confirmation {TRADE_ID}",
        issuer=bank["name"],
        trade_id=TRADE_ID,
        ours=OURS,
        cp=BANK,
        terms=terms | {"value_date": wrong_date.isoformat()},
        class_uri=deriv,
        version=2,
        extra=extra,
    )
    d.mail(
        "bank-ticket",
        b("trade_finance"),
        n("ops"),
        f"Our confirmation of today's forward is attached: you buy GBP {money(GBP_AMOUNT)} against USD {money(usd_amount)} at {RATE}.",
        ticket2,
        subject=f"Forward confirmation {TRADE_ID}",
        days=1,
        tags=["ticket", "planted"],
        doc=deriv,
        facts={"trade_id": TRADE_ID, "ticket_version": 2, "rate": num(RATE), "value_date": wrong_date.isoformat()},
    )
    d.mail(
        "ops-query",
        n("ops"),
        b("trade_finance"),
        f"Your confirmation shows value {wrong_date.isoformat()}. The deal was done for value {value_date.isoformat()}. Please correct it.",
        reply="bank-ticket",
        days=1,
        tags=["ticket", "query"],
        facts={"trade_id": TRADE_ID, "value_date": value_date.isoformat()},
    )
    ticket3 = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID}-bank-ticket-v3.pdf",
        title=f"Forward confirmation {TRADE_ID}",
        issuer=bank["name"],
        trade_id=TRADE_ID,
        ours=OURS,
        cp=BANK,
        terms=terms,
        class_uri=deriv,
        version=3,
        extra=extra,
    )
    d.mail(
        "bank-corrected",
        b("trade_finance"),
        n("ops"),
        f"Apologies, the value date was keyed wrongly. The corrected confirmation is attached, value {value_date.isoformat()}. It replaces the earlier one.",
        ticket3,
        reply="ops-query",
        days=1,
        tags=["ticket", "correction"],
        doc=deriv,
        facts={"trade_id": TRADE_ID, "ticket_version": 3, "rate": num(RATE), "value_date": value_date.isoformat()},
    )
    d.mail(
        "booked",
        n("ops"),
        n("trader"),
        f"Booked as {TRADE_ID} on the corrected confirmation: GBP {money(GBP_AMOUNT)} at {RATE}, value {value_date.isoformat()}.",
        days=1,
        subject=f"Booked {TRADE_ID}",
        tags=["booking"],
        facts={"trade_id": TRADE_ID},
    )
    d.mail(
        "settlement-instructions",
        n("settlements"),
        b("operations"),
        f"For {TRADE_ID}, please credit GBP {money(GBP_AMOUNT)} to account {gbp['number']} at {d.org(gbp['bank'])['name']} and debit USD {money(usd_amount)} from account {usd['number']} on {value_date.isoformat()}.",
        subject=f"Settlement instructions {TRADE_ID}",
        days=1,
        tags=["settlement", "ssi"],
        facts={
            "trade_id": TRADE_ID,
            "gbp_account": gbp["id"],
            "usd_account": usd["id"],
            "value_date": value_date.isoformat(),
        },
    )

    deal = deal_record(
        sc,
        onb,
        trade_id=TRADE_ID,
        kind="fx-forward",
        side="buy",
        terms=terms,
        notional=usd_amount,
        credit=None,
        class_uris={
            "transaction": txn,
            "instrument": deriv,
            "contract_type": fxout,
            "settlement_terms": cls("SettlementTerms"),
        },
        inputs={"buy_amount": GBP_AMOUNT, "rate": num(RATE)},
        chats=["dealing"],
        agreed_in="dealing",
        accounts={"gbp": gbp["id"], "usd": usd["id"]},
    )
    record_ticket(deal, "ticket", ticket1)
    record_ticket(deal, "bank-ticket", ticket2)
    record_ticket(deal, "bank-corrected", ticket3)
    record_mismatch(
        d,
        deal,
        "wrong-value-date",
        "wrong-value-date",
        "value_date",
        value_date.isoformat(),
        wrong_date.isoformat(),
        ["chat:dealing", "bank-ticket", ticket2.filename],
        "Northgate's first confirmation shows a value date one business day after the date dealt.",
        True,
        "ops-query",
    )
    finish(sc, [deal])
    sc.outcome = {
        "result": "agreed",
        "trade_id": TRADE_ID,
        "usd_amount": usd_amount,
        "value_date": value_date.isoformat(),
    }
    d.question(1, "hybrid_retrieval", f"At what rate did NewCo London buy GBP forward on {TRADE_ID}?", num(RATE))
    d.question(
        2,
        "reconciliation",
        f"What value date did Northgate's first confirmation of {TRADE_ID} show?",
        wrong_date.isoformat(),
    )
    d.question(3, "metadata", f"What USD amount settles {TRADE_ID}?", usd_amount)
    return sc
