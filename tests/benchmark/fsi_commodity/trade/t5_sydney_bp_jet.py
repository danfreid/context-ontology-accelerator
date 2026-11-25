# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""t5: NewCo Sydney sells 30,000 mt Jet A-1 CIF Brisbane to BP Australia; group exposure includes London's open USD 62m. BP's recap says CFR."""

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

SCENARIO_ID = "t5-sydney-bp-jet"
ONBOARDING_ID = "s5-sydney-bp-jet"
EXPECT = {"emails": 7, "attachments": 3, "chats": 2}
TRADE_ID = ids.trade_id("SYD", TRADE_YEAR, 3)

QUANTITY = 30_000
ASK, BID, PRICE = Decimal("812.00"), Decimal("788.00"), Decimal("795.00")  # USD per mt


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    onb = onboarded(u, upstream)[ONBOARDING_ID]
    ours, theirs, london = onb["entity"], onb["counterparty"], onb["london_entity"]
    sc = Scenario(
        SCENARIO_ID,
        "trade",
        u,
        start_clock(onb),
        expect=EXPECT,
        title="Sydney sells Jet A-1 to BP Australia: group exposure includes London's USD 62m, recap says CFR",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    me, cp = d.org(ours), d.org(theirs)
    window_from = business_date(d, ours, d.today(), 30)
    window_to = business_date(d, ours, window_from, 5)
    notional = int(QUANTITY * PRICE)
    check = credit_check(onb, notional, group_limit=onb["group_limit"], other_group_exposure=onb["london_exposure"])
    terms = {
        "product": "Jet A-1",
        "quantity": QUANTITY,
        "unit": "mt",
        "price_usd_per_mt": num(PRICE),
        "incoterm": "CIF",
        "location": "Brisbane",
        "currency": "USD",
        "delivery_from": window_from.isoformat(),
        "delivery_to": window_to.isoformat(),
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
        "indication",
        [
            say(
                v("trader"),
                f"Do you have {QUANTITY:,} mt of Jet A-1 for Brisbane around {window_from.isoformat()}, CIF?",
                "enquiry",
                product="Jet A-1",
                quantity=QUANTITY,
                unit="mt",
                incoterm="CIF",
                location="Brisbane",
            ),
            say(n("trader"), f"Yes. USD {ASK} per mt CIF Brisbane.", "offer", price_usd_per_mt=num(ASK)),
            say(v("trader"), f"USD {BID} and we can talk.", "counter", price_usd_per_mt=num(BID)),
        ],
        deal=TRADE_ID,
        tags=["indication"],
        class_uri=order,
    )
    d.sc.chat(
        "agreement",
        [
            say(n("trader"), f"USD {PRICE} per mt and it is done.", "counter", price_usd_per_mt=num(PRICE)),
            say(
                v("trader"),
                f"Agreed. {QUANTITY:,} mt Jet A-1 CIF Brisbane at USD {PRICE} per mt, delivery {window_from.isoformat()} to {window_to.isoformat()}, in USD. Subject to your credit.",
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
        f"We agreed to sell {QUANTITY:,} mt of Jet A-1 to {cp['name']} at USD {PRICE} per mt, USD {money(notional)}. Please check the Sydney sub-limit and the group limit.",
        subject=f"Credit check: {cp['name']} Jet A-1 sale {TRADE_ID}",
        tags=["credit"],
        facts={"trade_id": TRADE_ID, "deal_value": notional},
    )
    d.mail(
        "london-query",
        n("credit_analyst"),
        f"{london}:credit_analyst",
        f"Sydney is selling to {cp['name']} under the group limit of USD {money(onb['group_limit'])}. How much is London's open exposure to the BP group today?",
        subject="BP group exposure for a Sydney sale",
        days=1,
        tags=["credit", "group"],
    )
    d.mail(
        "london-reply",
        f"{london}:credit_analyst",
        n("credit_analyst"),
        f"London's open exposure to the BP group is USD {money(onb['london_exposure'])}.",
        reply="london-query",
        days=1,
        tags=["credit", "group"],
        facts={"london_exposure": onb["london_exposure"]},
    )
    d.mail(
        "credit-result",
        n("credit_analyst"),
        n("trader"),
        f"Approved. Sydney's sub-limit is USD {money(check['limit'])}, so headroom after is USD {money(check['headroom_after'])}. Group exposure is USD {money(check['group_exposure_before'])} before this sale and USD {money(check['group_exposure_after'])} after, against the USD {money(check['group_limit'])} group limit. The check sheet is attached.",
        credit_check_pdf(d, ours, theirs, TRADE_ID, check),
        reply="credit-request",
        days=1,
        tags=["credit", "approval", "group"],
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
        f"Deal ticket {TRADE_ID} is attached: sell {QUANTITY:,} mt Jet A-1 to {cp['name']}, CIF Brisbane, USD {PRICE} per mt. Please book it.",
        ticket,
        subject=f"Deal ticket {TRADE_ID}",
        days=1,
        tags=["ticket"],
        doc=txn,
        facts={
            "trade_id": TRADE_ID,
            "ticket_version": 1,
            "incoterm": "CIF",
            "quantity": QUANTITY,
            "price_usd_per_mt": num(PRICE),
        },
    )
    recap = deal_doc(
        d,
        kind="trade-recap",
        filename=f"{TRADE_ID}-recap-bp.pdf",
        title=f"Trade recap {TRADE_ID}",
        issuer=cp["name"],
        trade_id=TRADE_ID,
        ours=ours,
        cp=theirs,
        terms=terms | {"incoterm": "CFR"},
        class_uri=order,
    )
    d.mail(
        "recap",
        v("trader"),
        n("trader"),
        f"Our recap of the Jet A-1 trade is attached: {QUANTITY:,} mt CFR Brisbane at USD {PRICE} per mt, delivery {window_from.isoformat()} to {window_to.isoformat()}.",
        recap,
        subject=f"Recap {TRADE_ID}: Jet A-1",
        days=1,
        tags=["recap", "planted"],
        doc=order,
        facts={"trade_id": TRADE_ID, "incoterm": "CFR", "quantity": QUANTITY, "price_usd_per_mt": num(PRICE)},
    )
    d.mail(
        "recap-ack",
        n("trader"),
        v("trader"),
        "Thanks, recap received. Our ops team will take it from here.",
        reply="recap",
        days=1,
        tags=["recap"],
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
        chats=["indication", "agreement"],
        agreed_in="agreement",
        group={"limit": onb["group_limit"], "london_exposure": onb["london_exposure"], "london_entity": london},
    )
    record_ticket(deal, "ticket", ticket)
    record_mismatch(
        d,
        deal,
        "wrong-incoterm",
        "wrong-incoterm",
        "incoterm",
        "CIF",
        "CFR",
        ["chat:agreement", "recap", recap.filename],
        "BP's recap says CFR Brisbane; the chat agreed CIF Brisbane, so insurance cover is unclear.",
        False,
    )
    finish(sc, [deal])
    sc.outcome = {"result": "agreed", "trade_id": TRADE_ID, "group_exposure_after": check["group_exposure_after"]}
    d.question(1, "metadata", f"What was the group exposure to BP after {TRADE_ID}?", check["group_exposure_after"])
    d.question(2, "reconciliation", f"Which incoterm does BP's recap for {TRADE_ID} state?", "CFR")
    d.question(3, "hybrid_retrieval", f"Which incoterm was agreed in chat for {TRADE_ID}?", "CIF")
    return sc
