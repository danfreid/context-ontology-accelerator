# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""t3: NewCo Sydney sells 20,000 mt ULP 91 CFR Newcastle to Harbourline; a second cargo is declined by credit and resized to 18,000 mt."""

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
    record_ticket,
    start_clock,
)

SCENARIO_ID = "t3-sydney-harbourline-ulp"
ONBOARDING_ID = "s3-sydney-harbourline-ulp"
EXPECT = {"emails": 8, "attachments": 3, "chats": 3}
TRADE_ID_A = ids.trade_id("SYD", TRADE_YEAR, 1)
TRADE_ID_B = ids.trade_id("SYD", TRADE_YEAR, 2)

QUANTITY_A, QUANTITY_REQUESTED_B, QUANTITY_B = 20_000, 20_000, 18_000
PRICE = Decimal("765.00")  # USD per mt, both cargoes


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    onb = onboarded(u, upstream)[ONBOARDING_ID]
    ours, theirs = onb["entity"], onb["counterparty"]
    sc = Scenario(
        SCENARIO_ID,
        "trade",
        u,
        start_clock(onb),
        expect=EXPECT,
        title="Sydney sells ULP 91 to Harbourline: the second cargo is declined by credit and resized to 18,000 mt",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    me, cp = d.org(ours), d.org(theirs)
    line_item, txn, order = cls("SupplyChainTradeLineItem"), cls("TradeTransaction"), cls("Order")
    extra = {
        "agreement": onb["agreement"],
        "payment_days": onb["payment_days"],
        "quantity_tolerance_pct": onb["tolerance_pct"],
        "governing_law": onb["governing_law"],
    }

    def say(role: str, text: str, intent: str, **t: Any) -> ChatLine:
        return ChatLine(d.addr(role), text, intent, t)

    def terms_for(quantity: int, delivery_from: Any, delivery_to: Any) -> dict[str, Any]:
        return {
            "product": "ULP 91",
            "quantity": quantity,
            "unit": "mt",
            "price_usd_per_mt": num(PRICE),
            "incoterm": "CFR",
            "location": "Newcastle",
            "currency": "USD",
            "delivery_from": delivery_from.isoformat(),
            "delivery_to": delivery_to.isoformat(),
        }

    # Cargo 1
    a_from = business_date(d, ours, d.today(), 25)
    a_to = business_date(d, ours, a_from, 5)
    terms_a = terms_for(QUANTITY_A, a_from, a_to)
    value_a = int(QUANTITY_A * PRICE)
    check_a = credit_check(onb, value_a)
    d.sc.chat(
        "cargo-1",
        [
            say(
                v("trader"),
                f"I want {QUANTITY_A:,} mt of ULP 91 CFR Newcastle for delivery around {a_from.isoformat()}. Price?",
                "enquiry",
                product="ULP 91",
                quantity=QUANTITY_A,
                unit="mt",
                incoterm="CFR",
                location="Newcastle",
            ),
            say(n("trader"), f"USD {PRICE} per mt CFR Newcastle.", "offer", price_usd_per_mt=num(PRICE)),
            say(
                v("trader"),
                f"Agreed. {QUANTITY_A:,} mt ULP 91 CFR Newcastle at USD {PRICE} per mt, delivery {a_from.isoformat()} to {a_to.isoformat()}, in USD. Subject to your credit.",
                "agree",
                **terms_a,
            ),
        ],
        deal=TRADE_ID_A,
        tags=["negotiation", "agreed"],
        class_uri=order,
    )
    d.mail(
        "credit-request-a",
        n("trader"),
        n("credit_analyst"),
        f"We agreed to sell {QUANTITY_A:,} mt of ULP 91 to {cp['name']} at USD {PRICE} per mt, which is USD {money(value_a)}. Please check the limit.",
        subject=f"Credit check: {cp['name']} ULP cargo {TRADE_ID_A}",
        tags=["credit"],
        facts={"trade_id": TRADE_ID_A, "deal_value": value_a},
    )
    d.mail(
        "credit-ok-a",
        n("credit_analyst"),
        n("trader"),
        f"Approved. The limit is USD {money(check_a['limit'])} across the unsecured, guaranteed and SBLC tiers. Exposure before this cargo is USD {money(check_a['exposure_before'])}, so headroom after is USD {money(check_a['headroom_after'])}.",
        reply="credit-request-a",
        days=1,
        tags=["credit", "approval"],
        doc=cls("CreditFacility"),
        facts={"trade_id": TRADE_ID_A, **check_a},
    )
    ticket_a = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID_A}-ticket-v1.pdf",
        title=f"Deal ticket {TRADE_ID_A}",
        issuer=me["name"],
        trade_id=TRADE_ID_A,
        ours=ours,
        cp=theirs,
        terms=terms_a,
        class_uri=txn,
        version=1,
        extra=extra,
    )
    d.mail(
        "ticket-a",
        n("trader"),
        n("ops"),
        f"Deal ticket {TRADE_ID_A} is attached: sell {QUANTITY_A:,} mt ULP 91 to {cp['name']}, CFR Newcastle, USD {PRICE} per mt, payment {onb['payment_days']} days. Please book it.",
        ticket_a,
        subject=f"Deal ticket {TRADE_ID_A}",
        days=1,
        tags=["ticket"],
        doc=txn,
        facts={
            "trade_id": TRADE_ID_A,
            "ticket_version": 1,
            "price_usd_per_mt": num(PRICE),
            "quantity": QUANTITY_A,
            "payment_days": onb["payment_days"],
        },
    )

    # Cargo 2: requested at 20,000 mt, declined, resized to 18,000 mt
    d.sc.chat(
        "cargo-2-request",
        [
            say(
                v("trader"),
                "Same again please: another 20,000 mt, same price, one month after the first.",
                "enquiry",
                quantity=QUANTITY_REQUESTED_B,
                unit="mt",
                price_usd_per_mt=num(PRICE),
            ),
            say(
                n("trader"),
                "Agreed in principle, subject to credit. I will come back to you.",
                "hold",
                quantity=QUANTITY_REQUESTED_B,
            ),
        ],
        deal=TRADE_ID_B,
        tags=["negotiation", "subject-to-credit"],
        class_uri=order,
    )
    value_req = int(QUANTITY_REQUESTED_B * PRICE)
    declined = credit_check(onb, value_req, existing=check_a["credit_used"])
    max_qty = int(Decimal(declined["headroom_before"]) // PRICE)
    d.mail(
        "credit-request-b",
        n("trader"),
        n("credit_analyst"),
        f"{cp['name']} wants a second cargo of {QUANTITY_REQUESTED_B:,} mt at USD {PRICE} per mt, USD {money(value_req)}, on top of the first. Please check.",
        subject=f"Credit check: {cp['name']} second ULP cargo",
        days=1,
        tags=["credit"],
        facts={"trade_id": TRADE_ID_B, "deal_value": value_req},
    )
    d.mail(
        "credit-decline-b",
        n("credit_analyst"),
        n("trader"),
        f"Declined at {QUANTITY_REQUESTED_B:,} mt. Exposure before is USD {money(declined['exposure_before'])} and this cargo would take it to USD {money(declined['exposure_if_booked'])}, over the USD {money(declined['limit'])} limit. Headroom is USD {money(declined['headroom_before'])}, which covers at most {max_qty:,} mt at USD {PRICE}. The check sheet is attached.",
        credit_check_pdf(d, ours, theirs, TRADE_ID_B, declined, suffix="-declined"),
        reply="credit-request-b",
        days=1,
        tags=["credit", "decline"],
        doc=cls("CreditFacility"),
        facts={"trade_id": TRADE_ID_B, "max_quantity": max_qty, **declined},
    )
    b_from = business_date(d, ours, d.today(), 45)
    b_to = business_date(d, ours, b_from, 5)
    terms_b = terms_for(QUANTITY_B, b_from, b_to)
    value_b = int(QUANTITY_B * PRICE)
    check_b = credit_check(onb, value_b, existing=check_a["credit_used"])
    d.sc.chat(
        "cargo-2-resize",
        [
            say(
                n("trader"),
                f"Credit cannot cover {QUANTITY_REQUESTED_B:,} mt. I can do {QUANTITY_B:,} mt at the same price.",
                "counter",
                quantity=QUANTITY_B,
                price_usd_per_mt=num(PRICE),
            ),
            say(v("trader"), f"{QUANTITY_B:,} mt works for us.", "accept", quantity=QUANTITY_B),
            say(
                n("trader"),
                f"Agreed: {QUANTITY_B:,} mt ULP 91 CFR Newcastle at USD {PRICE} per mt, delivery {b_from.isoformat()} to {b_to.isoformat()}, in USD.",
                "agree",
                **terms_b,
            ),
        ],
        deal=TRADE_ID_B,
        tags=["negotiation", "agreed", "resized"],
        class_uri=order,
    )
    d.mail(
        "credit-ok-b",
        n("credit_analyst"),
        n("trader"),
        f"{QUANTITY_B:,} mt is approved. The cargo is USD {money(value_b)}, exposure after is USD {money(check_b['exposure_after'])} against the USD {money(check_b['limit'])} limit, and headroom after is USD {money(check_b['headroom_after'])}.",
        reply="credit-decline-b",
        days=1,
        tags=["credit", "approval"],
        doc=cls("CreditFacility"),
        facts={"trade_id": TRADE_ID_B, **check_b},
    )
    ticket_b = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID_B}-ticket-v1.pdf",
        title=f"Deal ticket {TRADE_ID_B}",
        issuer=me["name"],
        trade_id=TRADE_ID_B,
        ours=ours,
        cp=theirs,
        terms=terms_b,
        class_uri=txn,
        version=1,
        extra=extra,
    )
    d.mail(
        "ticket-b",
        n("trader"),
        n("ops"),
        f"Deal ticket {TRADE_ID_B} is attached: sell {QUANTITY_B:,} mt ULP 91 to {cp['name']}, CFR Newcastle, USD {PRICE} per mt. This is the second cargo, resized from {QUANTITY_REQUESTED_B:,} mt. Please book it.",
        ticket_b,
        subject=f"Deal ticket {TRADE_ID_B}",
        days=1,
        tags=["ticket", "resized"],
        doc=txn,
        facts={
            "trade_id": TRADE_ID_B,
            "ticket_version": 1,
            "price_usd_per_mt": num(PRICE),
            "quantity": QUANTITY_B,
            "resized_from": QUANTITY_REQUESTED_B,
        },
    )
    d.mail(
        "recap-b",
        n("trader"),
        v("trader"),
        f"Recap of the second cargo: {QUANTITY_B:,} mt ULP 91 CFR Newcastle at USD {PRICE} per mt, delivery {b_from.isoformat()} to {b_to.isoformat()}, payment {onb['payment_days']} days from invoice. This replaces the {QUANTITY_REQUESTED_B:,} mt we discussed.",
        subject=f"Recap {TRADE_ID_B}: ULP 91",
        days=1,
        tags=["recap", "resized"],
        doc=order,
        facts={
            "trade_id": TRADE_ID_B,
            "quantity": QUANTITY_B,
            "price_usd_per_mt": num(PRICE),
            "payment_days": onb["payment_days"],
        },
    )

    classes = {"transaction": txn, "line_item": line_item, "order": order}
    deal_a = deal_record(
        sc,
        onb,
        trade_id=TRADE_ID_A,
        kind="physical",
        side="sell",
        terms=terms_a,
        notional=value_a,
        credit=check_a,
        class_uris=classes,
        inputs={"quantity": QUANTITY_A, "price": num(PRICE)},
        chats=["cargo-1"],
        agreed_in="cargo-1",
    )
    record_ticket(deal_a, "ticket-a", ticket_a)
    deal_b = deal_record(
        sc,
        onb,
        trade_id=TRADE_ID_B,
        kind="physical",
        side="sell",
        terms=terms_b,
        notional=value_b,
        credit=check_b,
        class_uris=classes,
        status="resized",
        inputs={"quantity": QUANTITY_B, "price": num(PRICE)},
        chats=["cargo-2-request", "cargo-2-resize"],
        agreed_in="cargo-2-resize",
        requested_quantity=QUANTITY_REQUESTED_B,
        declined_checks=[declined],
        max_quantity=max_qty,
    )
    record_ticket(deal_b, "ticket-b", ticket_b)
    finish(sc, [deal_a, deal_b])
    sc.outcome = {
        "result": "second cargo declined at 20000 mt and resized",
        "trade_ids": [TRADE_ID_A, TRADE_ID_B],
        "exposure_after": check_b["exposure_after"],
        "headroom_after": check_b["headroom_after"],
    }
    d.question(
        1, "metadata", "At what quantity did credit decline Harbourline's second ULP cargo?", QUANTITY_REQUESTED_B
    )
    d.question(2, "metadata", "To what quantity was Harbourline's second ULP cargo resized?", QUANTITY_B)
    d.question(3, "metadata", f"How many days after invoice is payment due on {TRADE_ID_A}?", onb["payment_days"])
    return sc
