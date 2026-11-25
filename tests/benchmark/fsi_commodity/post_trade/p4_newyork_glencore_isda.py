# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""p4: NewCo New York pays fixed on a WTI swap. The confirmation follows a ticket that says 68.04; the margin call ignores the CSA threshold."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ..ontology import cls
from ..scenario import Scenario
from .common import (
    Desk,
    business_date,
    deal_of,
    dec,
    fmt,
    latest_ticket,
    num,
    onboarded,
    planted,
    register,
    trade_start,
    wait_until,
)
from .docs import confirmation, margin_call

SCENARIO_ID = "p4-newyork-glencore-isda"
TRADE_SCENARIO = "t4-newyork-glencore-isda"
EXPECT = {"emails": 7, "attachments": 4, "chats": 0}
FORWARD_PRICE = Decimal("63.20")  # USD per bbl, average forward price over the remaining term at the valuation date
COLLATERAL_HELD = Decimal("0")


def build(u: dict[str, Any], upstream: Any) -> Scenario:
    deal = deal_of(u, upstream, TRADE_SCENARIO)
    tid, terms = deal["trade_id"], deal["terms"]
    ours, theirs = deal["entity"], deal["counterparty"]
    onb = onboarded(u, upstream)[deal["onboarding_scenario"]]
    ticket = latest_ticket(tid, u, upstream)
    mismatch = planted(deal, "ticket-transposed-digits")
    agreed_fixed, ticket_fixed = dec(mismatch["agreed"]), dec(mismatch["found"])
    threshold, min_transfer = dec(onb["threshold"]), dec(onb["min_transfer"])
    sc = Scenario(
        SCENARIO_ID,
        "post_trade",
        u,
        trade_start(upstream, deal),
        expect=EXPECT,
        title="New York pays fixed on a WTI swap: confirmation on the mistyped ticket, margin call that ignores the CSA threshold",
    )
    d = Desk(sc)

    def n(role: str) -> str:
        return f"{ours}:{role}"

    def v(role: str) -> str:
        return f"{theirs}:{role}"

    volume = int(terms["notional_bbl_per_month"] * terms["months"])
    fixed_field = "fixed_price_usd_per_bbl"

    conf1 = confirmation(
        d,
        filename=f"{tid}-confirmation-v1.pdf",
        title=f"Swap confirmation {tid}",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        confirm_date=d.today(),
        terms=ticket["facts"],
        based_on=ticket["filename"],
        class_uri=cls("DerivativeInstrument"),
        extra=[("Fixed price payer", terms["fixed_payer"])],
    )
    d.mail(
        "confirmation",
        n("ops"),
        v("ops"),
        f"Please find the confirmation of {tid}, built from deal ticket version {ticket['version']}: NewCo pays fixed {ticket['facts'][fixed_field]:.2f}. Sign and return.",
        conf1,
        subject=f"Swap confirmation {tid}",
        tags=["confirmation", "planted"],
        doc=cls("DerivativeInstrument"),
        facts={"trade_id": tid, "ticket_version": ticket["version"], fixed_field: ticket["facts"][fixed_field]},
    )
    d.mail(
        "confirmation-query",
        v("ops"),
        n("ops"),
        f"We agreed {agreed_fixed:.2f} fixed. Your confirmation says {ticket_fixed:.2f}. We cannot sign it.",
        reply="confirmation",
        days=1,
        tags=["confirmation", "dispute"],
        facts={"trade_id": tid, "agreed": num(agreed_fixed), "confirmation": num(ticket_fixed)},
    )
    corrected_terms = dict(ticket["facts"]) | {fixed_field: terms[fixed_field]}
    conf2 = confirmation(
        d,
        filename=f"{tid}-confirmation-v2.pdf",
        title=f"Swap confirmation {tid} (corrected)",
        trade_id=tid,
        issuer_id=ours,
        to_id=theirs,
        confirm_date=d.today(),
        terms=corrected_terms,
        based_on=f"{ticket['filename']}, fixed price corrected to the agreed {agreed_fixed:.2f}",
        class_uri=cls("DerivativeInstrument"),
        extra=[("Fixed price payer", terms["fixed_payer"])],
    )
    d.mail(
        "confirmation-corrected",
        n("ops"),
        v("ops"),
        f"Corrected confirmation attached: fixed price {agreed_fixed:.2f}. The ticket had transposed digits.",
        conf2,
        reply="confirmation-query",
        days=1,
        tags=["confirmation", "correction"],
        doc=cls("DerivativeInstrument"),
        facts={"trade_id": tid, fixed_field: num(agreed_fixed)},
    )

    # --- margin call that ignores the threshold -----------------------------------------------
    start = date.fromisoformat(terms["start"])
    wait_until(d, v("ops"), business_date(d, ours, start, -1))
    valuation = d.today()
    transfer_by = business_date(d, ours, valuation, 1)
    common = {
        "trade_id": tid,
        "issuer_id": theirs,
        "to_id": ours,
        "valuation_date": valuation,
        "transfer_by": transfer_by,
        "fixed_price": dec(terms[fixed_field]),
        "forward_price": FORWARD_PRICE,
        "volume": volume,
        "collateral_held": COLLATERAL_HELD,
        "min_transfer": min_transfer,
        "currency": terms["currency"],
    }
    mc1, r1 = margin_call(
        d,
        filename=f"{tid}-margin-call-r1.pdf",
        number=f"MC-{tid}-1",
        threshold_applied=Decimal(0),
        revision=1,
        **common,
    )
    d.mail(
        "margin-call",
        v("ops"),
        n("settlements"),
        f"Variation margin call for {tid}: USD {fmt(r1['call'])} to be transferred by {transfer_by.isoformat()}.",
        mc1,
        subject=f"Margin call MC-{tid}-1",
        tags=["margin-call", "planted"],
        doc=cls("CLM"),
        facts={"trade_id": tid, "amount_called": num(r1["call"]), "exposure": num(r1["exposure"])},
    )
    d.mail(
        "margin-call-query",
        n("settlements"),
        v("ops"),
        f"Under the CSA the threshold is USD {fmt(threshold)} and the minimum transfer USD {fmt(min_transfer)}. Your call of USD {fmt(r1['call'])} on an exposure of USD {fmt(r1['exposure'])} applies no threshold. Please reissue.",
        reply="margin-call",
        days=1,
        tags=["margin-call", "dispute"],
        facts={"trade_id": tid, "threshold": num(threshold), "exposure": num(r1["exposure"])},
    )
    mc2, r2 = margin_call(
        d, filename=f"{tid}-margin-call-r2.pdf", number=f"MC-{tid}-2", threshold_applied=threshold, revision=2, **common
    )
    d.mail(
        "margin-call-revised",
        v("ops"),
        n("settlements"),
        f"Revised call attached. After the USD {fmt(threshold)} threshold the amount is USD {fmt(r2['call'])}, to be transferred by {transfer_by.isoformat()}.",
        mc2,
        reply="margin-call-query",
        days=1,
        tags=["margin-call", "correction"],
        doc=cls("CLM"),
        facts={"trade_id": tid, "amount_called": num(r2["call"]), "exposure": num(r2["exposure"])},
    )
    d.mail(
        "margin-call-ack",
        n("settlements"),
        n("ops"),
        f"Glencore's revised call for {tid} is USD {fmt(r2['call'])}. Treasury will move it by {transfer_by.isoformat()}.",
        subject=f"Margin call {tid} accepted",
        days=1,
        tags=["margin-call"],
        facts={"trade_id": tid, "amount_called": num(r2["call"])},
    )
    collateral_acct = onb["accounts"]["newco"]
    register(
        sc,
        trade_id=tid,
        kind="margin-call",
        filename=mc2.filename,
        message="margin-call-revised",
        number=mc2.facts["number"],
        issuer=theirs,
        payer=ours,
        payee=theirs,
        currency=terms["currency"],
        payable=num(r2["call"]),
        due=(transfer_by + timedelta(days=0)).isoformat(),
        payer_account=collateral_acct,
        payee_account=onb["accounts"]["counterparty"],
        inputs={"exposure": num(r2["exposure"]), "threshold": num(threshold), "collateral_held": num(COLLATERAL_HELD)},
    )
    d.contradiction(
        "confirmation-on-mistyped-ticket",
        "confirmation-vs-agreement",
        f"NewCo's confirmation follows ticket v{ticket['version']} with fixed price {ticket_fixed:.2f}; the agreed fixed price is {agreed_fixed:.2f}. Glencore refused to sign.",
        [ticket["filename"], conf1.filename, "chat:agreement"],
        {"field": fixed_field, "agreed": num(agreed_fixed), "found": num(ticket_fixed)},
        True,
        "confirmation-query",
    )
    d.contradiction(
        "margin-call-ignores-threshold",
        "margin-call-vs-csa",
        f"The first margin call applies no threshold: USD {fmt(r1['call'])} called; the CSA threshold of USD {fmt(threshold)} leaves USD {fmt(r2['call'])}.",
        [mc1.filename, onb["csa_agreement"]],
        {"field": "amount_called", "agreed": num(r2["call"]), "found": num(r1["call"]), "threshold": num(threshold)},
        True,
        "margin-call-query",
    )
    sc.outcome = {
        "result": "corrected",
        "trade_id": tid,
        "amount_called": num(r2["call"]),
        "first_call": num(r1["call"]),
        "exposure": num(r2["exposure"]),
    }
    d.question(
        1, "reconciliation", f"What fixed price did NewCo's first confirmation for {tid} state?", num(ticket_fixed)
    )
    d.question(
        2,
        "reconciliation",
        f"What amount should the margin call for {tid} be after the CSA threshold?",
        num(r2["call"]),
    )
    d.question(3, "metadata", f"What is the CSA threshold for {tid}?", num(threshold))
    return sc
