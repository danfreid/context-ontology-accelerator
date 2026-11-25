# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""t4: NewCo New York pays fixed 68.40 to Glencore on a 12-month WTI swap; the amended ticket says 68.04."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .. import ids
from ..ontology import cls
from ..scenario import ChatLine, Scenario
from .common import (
    TRADE_YEAR,
    Desk,
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

SCENARIO_ID = "t4-newyork-glencore-isda"
ONBOARDING_ID = "s4-newyork-glencore-isda"
EXPECT = {"emails": 7, "attachments": 3, "chats": 2}
TRADE_ID = ids.trade_id("NYC", TRADE_YEAR, 1)

MONTHLY_BBL, MONTHS = 50_000, 12
ASK, BID, FIXED = Decimal("68.90"), Decimal("68.20"), Decimal("68.40")
TYPED = Decimal("68.04")  # the amended ticket: last two digits of 68.40 transposed
PFE_PCT = 10  # potential future exposure add-on, as a percent of total fixed notional


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    onb = onboarded(u, upstream)[ONBOARDING_ID]
    ours, theirs = onb["entity"], onb["counterparty"]
    sc = Scenario(
        SCENARIO_ID,
        "trade",
        u,
        start_clock(onb),
        expect=EXPECT,
        title="New York swaps WTI with Glencore: fixed 68.40 agreed, amended ticket says 68.04",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    me, cp = d.org(ours), d.org(theirs)
    start = _add_months(d.today(), 2)
    end = _add_months(start, MONTHS) - timedelta(days=1)
    terms = {
        "instrument": "WTI fixed-for-floating swap",
        "fixed_payer": me["name"],
        "fixed_receiver": cp["name"],
        "fixed_price_usd_per_bbl": num(FIXED),
        "floating": "Platts WTI Cushing, monthly average",
        "notional_bbl_per_month": MONTHLY_BBL,
        "months": MONTHS,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "currency": "USD",
        "settlement": "monthly, in cash",
    }
    total_bbl = MONTHLY_BBL * MONTHS
    notional = int(total_bbl * FIXED)
    pfe = notional * PFE_PCT // 100
    check = credit_check(onb, pfe)
    txn, deriv = cls("TradeTransaction"), cls("DerivativeInstrument")
    swaps, underlier, settle = cls("SWAPS"), cls("Underlier"), cls("SettlementTerms")
    extra = {
        "agreement": onb["agreement"],
        "csa": onb["csa_agreement"],
        "csa_threshold_usd": onb["threshold"],
        "governing_law": onb["governing_law"],
    }

    def say(role: str, text: str, intent: str, **t: Any) -> ChatLine:
        return ChatLine(d.addr(role), text, intent, t)

    d.sc.chat(
        "indication",
        [
            say(
                n("trader"),
                f"I want to pay fixed on a {MONTHS}-month WTI swap, {MONTHLY_BBL:,} bbl a month against the Platts Cushing monthly average, starting {start.isoformat()}. Where do you receive?",
                "enquiry",
                instrument="WTI fixed-for-floating swap",
                notional_bbl_per_month=MONTHLY_BBL,
                months=MONTHS,
            ),
            say(v("trader"), f"I can receive {ASK} fixed.", "offer", fixed_price_usd_per_bbl=num(ASK)),
            say(n("trader"), f"I can pay {BID}. Think about it.", "counter", fixed_price_usd_per_bbl=num(BID)),
        ],
        deal=TRADE_ID,
        tags=["indication"],
        class_uri=swaps,
    )
    d.sc.chat(
        "execution",
        [
            say(v("trader"), f"{FIXED} is my best level to receive.", "counter", fixed_price_usd_per_bbl=num(FIXED)),
            say(n("trader"), "Done.", "accept", fixed_price_usd_per_bbl=num(FIXED)),
            say(
                v("trader"),
                f"Confirming: you pay {FIXED} fixed, we pay the Platts WTI Cushing monthly average, {MONTHLY_BBL:,} bbl a month for {MONTHS} months from {start.isoformat()} to {end.isoformat()}, settled monthly in USD cash. Subject to your credit.",
                "agree",
                **terms,
            ),
        ],
        deal=TRADE_ID,
        tags=["negotiation", "agreed"],
        class_uri=swaps,
    )
    d.mail(
        "credit-request",
        n("trader"),
        n("credit_analyst"),
        f"We agreed a {MONTHS}-month WTI swap with {cp['name']}: {MONTHLY_BBL:,} bbl a month, fixed {FIXED}, total fixed notional USD {money(notional)}. Please size the exposure.",
        subject=f"Credit check: {cp['name']} WTI swap {TRADE_ID}",
        tags=["credit"],
        facts={"trade_id": TRADE_ID, "total_notional": notional},
    )
    d.mail(
        "credit-result",
        n("credit_analyst"),
        n("trader"),
        f"Approved. I sized potential future exposure at {PFE_PCT}% of the total fixed notional, USD {money(pfe)}. The limit is USD {money(check['limit'])}, so headroom after is USD {money(check['headroom_after'])}. Under the CSA the threshold is USD {money(onb['threshold'])} and the minimum transfer USD {money(onb['min_transfer'])}. The check sheet is attached.",
        credit_check_pdf(d, ours, theirs, TRADE_ID, check),
        reply="credit-request",
        days=1,
        tags=["credit", "approval"],
        doc=cls("CreditFacility"),
        facts={"trade_id": TRADE_ID, "pfe_pct": PFE_PCT, **check},
    )
    ticket1 = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID}-ticket-v1.pdf",
        title=f"Deal ticket {TRADE_ID}",
        issuer=me["name"],
        trade_id=TRADE_ID,
        ours=ours,
        cp=theirs,
        terms=terms,
        class_uri=deriv,
        version=1,
        extra=extra,
    )
    d.mail(
        "ticket-v1",
        n("trader"),
        n("ops"),
        f"Deal ticket {TRADE_ID} is attached: pay fixed {FIXED} to {cp['name']} on a {MONTHS}-month WTI swap, {MONTHLY_BBL:,} bbl a month. Please book it.",
        ticket1,
        subject=f"Deal ticket {TRADE_ID}",
        days=1,
        tags=["ticket"],
        doc=deriv,
        facts={"trade_id": TRADE_ID, "ticket_version": 1, "fixed_price_usd_per_bbl": num(FIXED)},
    )
    d.mail(
        "booked-v1",
        n("ops"),
        n("trader"),
        f"Booked as {TRADE_ID}.",
        reply="ticket-v1",
        days=1,
        tags=["booking"],
        facts={"trade_id": TRADE_ID},
    )
    ticket2 = deal_doc(
        d,
        kind="deal-ticket",
        filename=f"{TRADE_ID}-ticket-v2.pdf",
        title=f"Deal ticket {TRADE_ID}",
        issuer=me["name"],
        trade_id=TRADE_ID,
        ours=ours,
        cp=theirs,
        terms=terms
        | {
            "fixed_price_usd_per_bbl": num(TYPED),
            "fixing": "last business day of each month",
            "payment": "five business days after each fixing",
        },
        class_uri=deriv,
        version=2,
        extra=extra,
    )
    d.mail(
        "ticket-v2",
        n("ops"),
        n("trader"),
        f"Amended ticket for {TRADE_ID} attached. It adds the monthly fixing and payment dates and replaces version 1. Other terms are unchanged.",
        ticket2,
        subject=f"Amended deal ticket {TRADE_ID}",
        days=1,
        tags=["ticket", "amendment", "planted"],
        doc=deriv,
        facts={"trade_id": TRADE_ID, "ticket_version": 2, "fixed_price_usd_per_bbl": num(TYPED)},
    )
    d.mail(
        "booked-v2",
        n("trader"),
        n("ops"),
        f"Thanks, please re-book {TRADE_ID} on version 2.",
        reply="ticket-v2",
        days=1,
        tags=["booking"],
        facts={"trade_id": TRADE_ID},
    )
    d.mail(
        "glencore-note",
        v("trader"),
        n("trader"),
        f"For our records: {MONTHS} months, {MONTHLY_BBL:,} bbl a month, you pay {FIXED} fixed. Our confirmation team will write to yours.",
        subject=f"WTI swap {TRADE_ID}: our record",
        days=1,
        tags=["recap"],
        facts={"trade_id": TRADE_ID, "fixed_price_usd_per_bbl": num(FIXED), "notional_bbl_per_month": MONTHLY_BBL},
    )

    deal = deal_record(
        sc,
        onb,
        trade_id=TRADE_ID,
        kind="swap",
        side="pay-fixed",
        terms=terms,
        notional=notional,
        credit=check,
        class_uris={
            "transaction": txn,
            "instrument": deriv,
            "contract_type": swaps,
            "underlier": underlier,
            "settlement_terms": settle,
        },
        inputs={"notional_bbl_per_month": MONTHLY_BBL, "months": MONTHS, "fixed_price": num(FIXED), "pfe_pct": PFE_PCT},
        chats=["indication", "execution"],
        agreed_in="execution",
        csa_threshold=onb["threshold"],
        csa_min_transfer=onb["min_transfer"],
    )
    record_ticket(deal, "ticket-v1", ticket1)
    record_ticket(deal, "ticket-v2", ticket2)
    record_mismatch(
        d,
        deal,
        "ticket-transposed-digits",
        "transposed-digits",
        "fixed_price_usd_per_bbl",
        num(FIXED),
        num(TYPED),
        ["chat:execution", "ticket-v2", ticket2.filename],
        f"The amended ticket carries a fixed price of {TYPED}; the agreed price was {FIXED} (last two digits transposed).",
        False,
    )
    finish(sc, [deal])
    sc.outcome = {"result": "agreed", "trade_id": TRADE_ID, "agreed_fixed": num(FIXED), "ticket_fixed": num(TYPED)}
    d.question(1, "hybrid_retrieval", f"What fixed price did NewCo agree to pay Glencore on {TRADE_ID}?", num(FIXED))
    d.question(2, "reconciliation", f"What fixed price does the latest ticket for {TRADE_ID} show?", num(TYPED))
    d.question(3, "metadata", f"What is the CSA threshold with Glencore recorded for {TRADE_ID}?", onb["threshold"])
    return sc
