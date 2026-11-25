# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""t2: NewCo London sells 8,000 mt VLSFO FOB Rotterdam to Kestrel. No SBLC, so prepayment; the first prepayment request names the wrong currency."""

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
    credit_check,
    credit_check_pdf,
    deal_doc,
    deal_record,
    finish,
    form,
    money,
    num,
    onboarded,
    record_mismatch,
    record_ticket,
    start_clock,
)

SCENARIO_ID = "t2-london-kestrel-vlsfo"
ONBOARDING_ID = "s2-london-kestrel-vlsfo"
EXPECT = {"emails": 7, "attachments": 3, "chats": 2}
TRADE_ID = ids.trade_id("LON", TRADE_YEAR, 1)

QUANTITY = 8_000
OPENING_ASK, KESTREL_BID, PRICE = Decimal("552.00"), Decimal("538.00"), Decimal("545.00")  # USD per mt


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    onb = onboarded(u, upstream)[ONBOARDING_ID]
    ours, theirs = onb["entity"], onb["counterparty"]
    sc = Scenario(
        SCENARIO_ID,
        "trade",
        u,
        start_clock(onb),
        expect=EXPECT,
        title="London sells 8,000 mt VLSFO to Kestrel: prepayment because no SBLC, wrong currency on the first request",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    me, cp = d.org(ours), d.org(theirs)
    window_from = business_date(d, ours, d.today(), 20)
    window_to = business_date(d, ours, window_from, 4)
    notional = int(QUANTITY * PRICE)
    check = credit_check(onb, notional)
    terms = {
        "product": "VLSFO",
        "quantity": QUANTITY,
        "unit": "mt",
        "price_usd_per_mt": num(PRICE),
        "incoterm": "FOB",
        "location": "Rotterdam",
        "currency": "USD",
        "loading_from": window_from.isoformat(),
        "loading_to": window_to.isoformat(),
        "payment": "prepayment of 100% of the cargo value before vessel nomination",
    }
    line_item, txn, order = cls("SupplyChainTradeLineItem"), cls("TradeTransaction"), cls("Order")
    extra = {
        "agreement": onb["agreement"],
        "payment_days": onb["payment_days"],
        "quantity_tolerance_pct": onb["tolerance_pct"],
        "governing_law": onb["governing_law"],
    }

    def say(role: str, text: str, intent: str, **t: Any) -> ChatLine:
        return ChatLine(d.addr(role), text, intent, t)

    d.sc.chat(
        "enquiry",
        [
            say(
                v("trader"),
                f"I need {QUANTITY:,} mt of VLSFO FOB Rotterdam for loading around {window_from.isoformat()}. What is your price?",
                "enquiry",
                product="VLSFO",
                quantity=QUANTITY,
                unit="mt",
                incoterm="FOB",
                location="Rotterdam",
            ),
            say(n("trader"), f"USD {OPENING_ASK} per mt.", "offer", price_usd_per_mt=num(OPENING_ASK)),
            say(v("trader"), f"I was thinking USD {KESTREL_BID}.", "counter", price_usd_per_mt=num(KESTREL_BID)),
        ],
        deal=TRADE_ID,
        tags=["indication"],
        class_uri=order,
    )
    d.sc.chat(
        "price",
        [
            say(
                n("trader"),
                f"USD {PRICE} per mt and I will keep it for the hour.",
                "counter",
                price_usd_per_mt=num(PRICE),
            ),
            say(v("trader"), "Done. Please send the recap.", "accept", price_usd_per_mt=num(PRICE)),
            say(
                n("trader"),
                f"Agreed: {QUANTITY:,} mt VLSFO FOB Rotterdam at USD {PRICE} per mt, loading {window_from.isoformat()} to {window_to.isoformat()}. You have no confirmed standby letter of credit yet, so payment is a prepayment of the full cargo value before nomination. Subject to our credit.",
                "agree",
                **terms,
            ),
        ],
        deal=TRADE_ID,
        tags=["negotiation", "agreed", "prepayment"],
        class_uri=order,
    )
    d.mail(
        "credit-request",
        n("trader"),
        n("credit_analyst"),
        f"We agreed to sell {QUANTITY:,} mt of VLSFO to {cp['name']} at USD {PRICE} per mt, which is USD {money(notional)}. Please check the position.",
        subject=f"Credit check: {cp['name']} VLSFO sale {TRADE_ID}",
        tags=["credit"],
        facts={"trade_id": TRADE_ID, "deal_value": notional},
    )
    d.mail(
        "credit-result",
        n("credit_analyst"),
        n("trader"),
        f"{cp['name']} has no open limit: approval was secured-only and no confirmed SBLC is in place. USD {money(onb['conditional_limit'])} opens once one is. This deal of USD {money(notional)} must be prepaid in full before nomination, so it uses no credit. The check sheet is attached.",
        credit_check_pdf(d, ours, theirs, TRADE_ID, check),
        reply="credit-request",
        days=1,
        tags=["credit", "prepayment"],
        doc=cls("CreditFacility"),
        facts={"trade_id": TRADE_ID, **check},
    )
    ticket = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID}-ticket-v1.pdf",
        title=f"Deal ticket {TRADE_ID}",
        issuer=me["name"],
        trade_id=TRADE_ID,
        ours=ours,
        cp=theirs,
        terms=terms,
        class_uri=txn,
        version=1,
        extra=extra,
    )
    d.mail(
        "ticket",
        n("trader"),
        n("ops"),
        f"Deal ticket {TRADE_ID} is attached: sell {QUANTITY:,} mt VLSFO to {cp['name']}, FOB Rotterdam, USD {PRICE} per mt, prepayment before nomination. Please book it.",
        ticket,
        subject=f"Deal ticket {TRADE_ID}",
        days=1,
        tags=["ticket"],
        doc=txn,
        facts={"trade_id": TRADE_ID, "ticket_version": 1, "price_usd_per_mt": num(PRICE), "quantity": QUANTITY},
    )
    d.mail(
        "recap",
        n("trader"),
        v("trader"),
        f"Recap of today's trade: we sell {QUANTITY:,} mt VLSFO FOB Rotterdam at USD {PRICE} per mt, loading {window_from.isoformat()} to {window_to.isoformat()}. Payment is a prepayment of USD {money(notional)} before nomination. Settlements will send the payment request.",
        subject=f"Recap {TRADE_ID}: VLSFO",
        days=1,
        tags=["recap"],
        doc=order,
        facts={"trade_id": TRADE_ID, "price_usd_per_mt": num(PRICE), "quantity": QUANTITY, "currency": "USD"},
    )
    usd = u["accounts"][onb["accounts"]["newco"]]
    gbp = next(a for a in u["accounts"].values() if a["owner"] == ours and a["currency"] == "GBP")
    due = business_date(d, ours, d.today(), 4)

    def request(acct: dict[str, Any], currency: str, filename: str) -> Any:
        return form(
            filename,
            "prepayment-request",
            f"Prepayment request: {TRADE_ID}",
            [
                ("Trade id", TRADE_ID),
                ("Payer", cp["name"]),
                ("Amount", f"{currency} {money(notional)}"),
                ("Due", due.isoformat()),
                ("Bank", d.org(acct["bank"])["name"]),
                ("Account", acct["number"]),
                ("IBAN", acct["iban"] or ""),
                ("BIC", acct["bic"]),
            ],
            cls("Invoice"),
            {
                "trade_id": TRADE_ID,
                "amount": notional,
                "currency": currency,
                "account": acct["id"],
                "due": due.isoformat(),
            },
        )

    d.mail(
        "prepay-request",
        n("settlements"),
        v("settlements"),
        f"Please prepay the cargo for {TRADE_ID} by {due.isoformat()}. The request is attached.",
        request(gbp, "GBP", f"{TRADE_ID}-prepayment-request.pdf"),
        subject=f"Prepayment request {TRADE_ID}",
        days=1,
        tags=["prepayment", "planted"],
        doc=cls("Invoice"),
        facts={"trade_id": TRADE_ID, "amount": notional, "currency": "GBP", "account": gbp["id"]},
    )
    d.mail(
        "prepay-query",
        v("settlements"),
        n("settlements"),
        f"Your request asks for GBP {money(notional)} into a sterling account. The deal is in US dollars. Please reissue the request in USD.",
        reply="prepay-request",
        days=1,
        tags=["prepayment", "query"],
        facts={"trade_id": TRADE_ID, "currency": "USD"},
    )
    d.mail(
        "prepay-corrected",
        n("settlements"),
        v("settlements"),
        f"You are right, the request should have been in USD. Please pay USD {money(notional)} by {due.isoformat()} to {d.org(usd['bank'])['name']}, account {usd['number']}, IBAN {usd['iban']}, BIC {usd['bic']}. Please ignore the sterling request.",
        reply="prepay-query",
        days=1,
        tags=["prepayment", "correction"],
        facts={
            "trade_id": TRADE_ID,
            "amount": notional,
            "currency": "USD",
            "account": usd["id"],
            "due": due.isoformat(),
        },
    )

    deal = deal_record(
        sc,
        onb,
        trade_id=TRADE_ID,
        kind="physical",
        side="sell",
        terms=terms,
        notional=notional,
        credit=check,
        class_uris={"transaction": txn, "line_item": line_item, "order": order},
        inputs={"quantity": QUANTITY, "price": num(PRICE)},
        chats=["enquiry", "price"],
        agreed_in="price",
        prepayment={"amount": notional, "currency": "USD", "due": due.isoformat(), "account": usd["id"]},
    )
    record_ticket(deal, "ticket", ticket)
    record_mismatch(
        d,
        deal,
        "wrong-currency-requested",
        "wrong-currency",
        "currency",
        "USD",
        "GBP",
        ["chat:price", "prepay-request", f"{TRADE_ID}-prepayment-request.pdf"],
        "The first prepayment request asks for GBP into the sterling account; the deal is in USD.",
        True,
        "prepay-query",
    )
    finish(sc, [deal])
    sc.outcome = {"result": "agreed", "trade_id": TRADE_ID, "prepayment": notional, "credit_used": 0}
    d.question(1, "metadata", f"What prepayment did NewCo London require from Kestrel for {TRADE_ID}?", notional)
    d.question(
        2,
        "reconciliation",
        f"In which currency did the first prepayment request for {TRADE_ID} ask for payment?",
        "GBP",
    )
    d.question(3, "hybrid_retrieval", f"What price per mt was agreed for {TRADE_ID}?", num(PRICE))
    return sc
