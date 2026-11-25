# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""t1: NewCo Houston buys 250 kbbl WTI Midland FOB Corpus Christi from Vitol. Premium 1.10 to 0.85 in chat; the recap says 0.95."""

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
    money,
    num,
    onboarded,
    record_mismatch,
    record_ticket,
    start_clock,
)

SCENARIO_ID = "t1-houston-vitol-wti"
ONBOARDING_ID = "s1-houston-vitol-wti"
EXPECT = {"emails": 7, "attachments": 3, "chats": 3}
TRADE_ID = ids.trade_id("HOU", TRADE_YEAR, 1)

QUANTITY = 250_000
REFERENCE_PRICE = Decimal("66.00")  # USD per bbl, used only to size the credit check
FIRST_OFFER, OPENING_BID, SECOND_OFFER, AGREED = Decimal("1.10"), Decimal("0.75"), Decimal("0.95"), Decimal("0.85")


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    onb = onboarded(u, upstream)[ONBOARDING_ID]
    ours, theirs = onb["entity"], onb["counterparty"]
    sc = Scenario(
        SCENARIO_ID,
        "trade",
        u,
        start_clock(onb),
        expect=EXPECT,
        title="Houston buys 250 kbbl WTI from Vitol: premium haggled in chat, recap carries the wrong premium",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    me, cp = d.org(ours), d.org(theirs)
    window_from = business_date(d, ours, d.today(), 25)
    window_to = business_date(d, ours, window_from, 7)
    terms = {
        "product": "WTI Midland",
        "quantity": QUANTITY,
        "unit": "bbl",
        "pricing": "Platts WTI Midland, average of the five assessments around the loading date, plus premium",
        "premium_usd_per_bbl": num(AGREED),
        "incoterm": "FOB",
        "location": "Corpus Christi",
        "currency": "USD",
        "loading_from": window_from.isoformat(),
        "loading_to": window_to.isoformat(),
    }
    notional = int(QUANTITY * (REFERENCE_PRICE + AGREED))
    check = credit_check(onb, notional)
    line_item, txn, order = cls("SupplyChainTradeLineItem"), cls("TradeTransaction"), cls("Order")
    extra = {
        "agreement": onb["agreement"],
        "payment_days": onb["payment_days"],
        "quantity_tolerance_pct": onb["tolerance_pct"],
        "governing_law": onb["governing_law"],
    }

    def say(role: str, text: str, intent: str, **t: Any) -> ChatLine:
        return ChatLine(d.addr(role), text, intent, t)

    loading = f"{window_from.strftime('%B %Y')}"
    d.sc.chat(
        "enquiry",
        [
            say(
                n("trader"),
                f"Morning. I want {QUANTITY // 1000}k bbl of WTI Midland FOB Corpus Christi, loading in {loading}. Where is your premium?",
                "enquiry",
                product="WTI Midland",
                quantity=QUANTITY,
                unit="bbl",
                incoterm="FOB",
                location="Corpus Christi",
            ),
            say(
                v("trader"),
                f"Premium is {FIRST_OFFER} over the five-day Platts average.",
                "offer",
                premium_usd_per_bbl=num(FIRST_OFFER),
            ),
            say(n("trader"), "Too high for me. Let me think about it and come back to you.", "hold"),
        ],
        deal=TRADE_ID,
        tags=["indication"],
        class_uri=order,
    )
    d.sc.chat(
        "premium",
        [
            say(
                n("trader"),
                f"I can do {OPENING_BID} over and we trade now.",
                "counter",
                premium_usd_per_bbl=num(OPENING_BID),
            ),
            say(v("trader"), f"{SECOND_OFFER} and it is yours.", "counter", premium_usd_per_bbl=num(SECOND_OFFER)),
            say(n("trader"), f"Meet me at {AGREED}.", "counter", premium_usd_per_bbl=num(AGREED)),
            say(
                v("trader"),
                f"Done at {AGREED}. {QUANTITY:,} bbl WTI Midland FOB Corpus Christi, Platts five-day average plus {AGREED}, loading {window_from.isoformat()} to {window_to.isoformat()}, in USD. Subject to your credit.",
                "agree",
                **terms,
            ),
        ],
        deal=TRADE_ID,
        tags=["negotiation", "agreed"],
        class_uri=order,
    )
    d.mail(
        "credit-request",
        n("trader"),
        n("credit_analyst"),
        f"We agreed to buy {QUANTITY:,} bbl of WTI Midland from {cp['name']}, subject to credit. At a reference price of USD {REFERENCE_PRICE} plus the premium the value is USD {money(notional)}. Please check the limit.",
        subject=f"Credit check: {cp['name']} WTI purchase {TRADE_ID}",
        tags=["credit"],
        facts={"trade_id": TRADE_ID, "deal_value": notional},
    )
    d.mail(
        "credit-ok",
        n("credit_analyst"),
        n("trader"),
        f"Approved. The limit is USD {money(check['limit'])}, exposure before this deal is USD {money(check['exposure_before'])} and the deal adds USD {money(notional)}, so headroom after is USD {money(check['headroom_after'])}. The check sheet is attached.",
        credit_check_pdf(d, ours, theirs, TRADE_ID, check),
        reply="credit-request",
        days=1,
        tags=["credit", "approval"],
        doc=cls("CreditFacility"),
        facts={"trade_id": TRADE_ID, **check},
    )
    recap = deal_doc(
        d,
        kind="trade-recap",
        filename=f"{TRADE_ID}-recap-vitol.pdf",
        title=f"Trade recap {TRADE_ID}",
        issuer=cp["name"],
        trade_id=TRADE_ID,
        ours=ours,
        cp=theirs,
        terms=terms | {"premium_usd_per_bbl": num(SECOND_OFFER)},
        class_uri=order,
    )
    d.mail(
        "recap",
        v("trader"),
        n("trader"),
        f"Our recap of today's trade is attached: {QUANTITY:,} bbl WTI Midland FOB Corpus Christi, premium {SECOND_OFFER} per bbl, loading {window_from.isoformat()} to {window_to.isoformat()}. Please confirm.",
        recap,
        subject=f"Recap {TRADE_ID}: WTI Midland",
        days=1,
        tags=["recap", "planted"],
        doc=order,
        facts={"trade_id": TRADE_ID, "premium_usd_per_bbl": num(SECOND_OFFER), "quantity": QUANTITY},
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
        f"Deal ticket {TRADE_ID} is attached: buy {QUANTITY:,} bbl WTI Midland from {cp['name']}, FOB Corpus Christi, premium {AGREED} per bbl. Please book it.",
        ticket,
        subject=f"Deal ticket {TRADE_ID}",
        days=1,
        tags=["ticket"],
        doc=txn,
        facts={"trade_id": TRADE_ID, "ticket_version": 1, "premium_usd_per_bbl": num(AGREED), "quantity": QUANTITY},
    )
    d.mail(
        "booked",
        n("ops"),
        n("trader"),
        f"Booked as {TRADE_ID} per the ticket: {QUANTITY:,} bbl at a premium of {AGREED}.",
        reply="ticket",
        days=1,
        tags=["booking"],
        facts={"trade_id": TRADE_ID},
    )
    d.sc.chat(
        "logistics",
        [
            say(
                n("trader"),
                "Recap received. We will nominate the vessel ten days before the window opens.",
                "logistics",
                nomination_notice_days=10,
            ),
            say(v("trader"), "Fine. Send the nomination to my ops team.", "logistics"),
        ],
        deal=TRADE_ID,
        tags=["logistics"],
        class_uri=order,
    )
    d.mail(
        "recap-ack",
        n("trader"),
        v("trader"),
        "Thanks for the recap. We will nominate the vessel as we discussed.",
        reply="recap",
        days=1,
        tags=["recap"],
    )
    d.mail(
        "handover",
        v("ops"),
        n("ops"),
        f"For {TRADE_ID}, please send the vessel nomination to us at least ten days before {window_from.isoformat()}.",
        subject=f"Nomination for {TRADE_ID}",
        days=1,
        tags=["logistics"],
        facts={"trade_id": TRADE_ID, "loading_from": window_from.isoformat()},
    )

    deal = deal_record(
        sc,
        onb,
        trade_id=TRADE_ID,
        kind="physical",
        side="buy",
        terms=terms,
        notional=notional,
        credit=check,
        class_uris={"transaction": txn, "line_item": line_item, "order": order},
        inputs={"quantity": QUANTITY, "reference_price": num(REFERENCE_PRICE), "premium": num(AGREED)},
        chats=["enquiry", "premium", "logistics"],
        agreed_in="premium",
    )
    record_ticket(deal, "ticket", ticket)
    record_mismatch(
        d,
        deal,
        "recap-differs-from-chat",
        "recap-vs-chat",
        "premium_usd_per_bbl",
        num(AGREED),
        num(SECOND_OFFER),
        ["chat:premium", "recap", recap.filename],
        f"Vitol's recap states a premium of {SECOND_OFFER}, a mid-negotiation offer; the chat settled at {AGREED}.",
        False,
    )
    finish(sc, [deal])
    sc.outcome = {"result": "agreed", "trade_id": TRADE_ID, "premium": num(AGREED), "recap_premium": num(SECOND_OFFER)}
    d.question(1, "hybrid_retrieval", f"What premium did the traders agree in chat for {TRADE_ID}?", num(AGREED))
    d.question(2, "reconciliation", f"What premium does Vitol's recap for {TRADE_ID} state?", num(SECOND_OFFER))
    d.question(
        3, "metadata", f"What was NewCo Houston's headroom with Vitol Inc. after {TRADE_ID}?", check["headroom_after"]
    )
    return sc
